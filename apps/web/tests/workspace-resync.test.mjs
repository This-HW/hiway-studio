// Reconnect resync re-reads durable job state (review B6-M1).
//
//   node tests/workspace-resync.test.mjs
//
// Runs the REAL stores/workspace.ts and stores/board.ts (transpiled with the
// installed `typescript` package; zustand and react are the installed
// packages) against a local 127.0.0.1 API stub. Scenario: jobs were tracked
// as queued/running from job.progress events, then the SSE stream dropped and
// their completion events were missed. After resync():
//   1. every tracked active job was read from its durable endpoint (generation
//      /jobs/{id}, batch parent, TTS /audio/{id}, render /timelines/{id});
//   2. settled rows become completed/failed/canceled, a still-running row
//      stays running, and a failed read leaves the job as it was;
//   3. a finished render is recorded in renders[timeline_id];
//   4. the board's takesSettled() turns true for the shot whose takes finished
//      (so the board reloads instead of staying pending);
//   5. eventSeq and resyncSeq both move.
//   6. (B6R2-M1) takes accepted over HTTP by the real board.generateFrame()
//      while no SSE event ever arrived are tracked and settled by resync;
//   7. (B6R2-M2) a resync GET that answers late (attempt A "failed") never
//      overwrites a newer SSE state (manual retry -> completed) that arrived
//      while the request was in flight.
//   8. (B6R3-M1) a take's terminal SSE event arrives BEFORE its HTTP 202:
//      once the board registers the pending take, eventSeq moves again so the
//      screen's settle effect (keyed on eventSeq) sees it settled.
//   9. (B6R3-R2-M1) a reconnect resync runs and finishes WHILE the job-
//      creating POST is in flight (it cannot see the new take); the late
//      track() reads the take's durable state once, so it does not stay
//      queued.
//  10. (B6R3-R2-M2) the character master / sheet settle helpers remove
//      canceled jobs from pending (completed and failed as before).
//  11. (C13) "재시도" on a failed take: board.retryTake() POSTs
//      /api/v1/jobs/{id}/retry, the take is tracked as queued again and
//      registered as pending, and its next SSE event settles it.
//  12. (C13) an SSE event for the new attempt that arrives while the retry
//      POST is in flight is newer than the 202: it is not reset to queued.
//  13. (C13) "취소" never marks a take canceled before the server confirms:
//      while POST /cancel is in flight and when the durable read still says
//      running, the take stays running; job.canceled (or a durable read that
//      says canceled) settles it.
//  14. (C13) a rejected cancel (400: the job finished meanwhile) toasts a
//      Korean error and settles the job from the server's state, not canceled.
//  15. (B10-H1) a resync GET of the OLD attempt ("failed") that lands while
//      the retry POST is in flight does not suppress the re-queue: after the
//      202 the job is queued (not settled) and the next resync re-reads it.
//      (Scenario 12 is the other direction: a new-attempt SSE is kept.)
//  16. (B10-M2) an accepted cancel re-reads the job even when the local state
//      is an OLD terminal one (failed): the server's canceled — or running —
//      replaces it; never an optimistic canceled.
//  17. (B10-M3) a retry rejected with 400 (another tab retried first) reads
//      the job and applies the server's ACTIVE state instead of staying failed.
//  18. (B10-M1) a header (workspace-only) retry reloads the board shot whose
//      take card shows that job, once per job change, and again on completion.
//  19. (B10-M4) an accepted frame generation reloads the open shot's takes so
//      the new queued take (and its "취소") appears before the batch settles.
//  20. (B10-M5) header rows label same-type jobs apart (type + short id).
//  21. (B10R2-M1) a late job.failed of the OLD attempt that arrives while the
//      retry POST is in flight does not leave the job failed: after the 202
//      it is queued and then takes the server's state; an SSE event after
//      the 202 still wins over that verification read.
//  22. (B10R2-M2) two overlapping shot-detail loads answered out of order:
//      only the latest request's takes are applied.
//  23. (B10R3-M1) old attempt's job.progress DURING the retry POST, then its
//      job.failed AFTER the 202, server running: the failed event is only
//      provisional (verifying, pending take not settled) and the server's
//      running wins. A genuine new-attempt failure inside the window (server
//      failed) ends failed and settles once confirmed.
//  24. (B10R4-M1) verification is fail-closed: a failed server check keeps
//      the job verifying (take not settled) and is retried; a later
//      successful retry applies the server's state; when every retry fails
//      the job stays unsettled with one Korean toast, and a reconnect resync
//      verifies it.
// Exits non-zero on any failure.
import { createRequire } from "node:module";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB = path.resolve(HERE, "..");
const require = createRequire(import.meta.url);
const ts = require(path.join(WEB, "node_modules", "typescript"));

// --- tiny CommonJS loader for stores/*.ts and lib/*.ts ------------------------
const moduleCache = new Map();
function loadTs(file) {
  if (moduleCache.has(file)) return moduleCache.get(file).exports;
  const src = fs.readFileSync(file, "utf8");
  const { outputText } = ts.transpileModule(src, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.React,
      esModuleInterop: true,
    },
    fileName: file,
  });
  const mod = { exports: {} };
  moduleCache.set(file, mod);
  const localRequire = (spec) => {
    let target = null;
    if (spec.startsWith("@/")) target = path.join(WEB, spec.slice(2));
    else if (spec.startsWith(".")) target = path.resolve(path.dirname(file), spec);
    if (target) {
      for (const cand of [target, `${target}.ts`, `${target}.tsx`, path.join(target, "index.ts")]) {
        if (fs.existsSync(cand) && fs.statSync(cand).isFile()) return loadTs(cand);
      }
      throw new Error(`cannot resolve ${spec} from ${file}`);
    }
    return require(path.join(WEB, "node_modules", spec));
  };
  new Function("exports", "require", "module", "__filename", "__dirname", outputText)(
    mod.exports, localRequire, mod, file, path.dirname(file)
  );
  return mod.exports;
}

// --- API stub -------------------------------------------------------------------
const ROUTES = {
  "/api/v1/jobs/gen-done": [200, { id: "gen-done", status: "succeeded" }],
  "/api/v1/jobs/gen-take2": [200, { id: "gen-take2", status: "failed", error_code: "PROVIDER_ERROR", error_message: "boom" }],
  "/api/v1/jobs/batch-1": [200, { batch: { id: "batch-1", status: "canceled" }, children: [] }],
  "/api/v1/jobs/gen-running": [200, { id: "gen-running", status: "running" }],
  "/api/v1/jobs/gen-broken": [500, { code: "INTERNAL", message: "x", retryable: true }],
  "/api/v1/audio/tts-1": [200, { id: "tts-1", status: "done" }],
  "/api/v1/timelines/tl-1": [200, { id: "tl-1", status: "done", output_path: "projects/p/out.mp4" }],
  // Scenario 6: POST accepted, batch detail lists the takes, takes finished.
  "POST /api/v1/shots/shot-2/frame": [202, { job_id: "batch-2", estimated_cost_usd: "0.0100" }],
  "/api/v1/jobs/batch-2": [200, {
    batch: { id: "batch-2", status: "running" },
    children: [{ id: "take-a", status: "running" }, { id: "take-b", status: "running" }],
  }],
  "/api/v1/jobs/take-a": [200, { id: "take-a", status: "succeeded" }],
  "/api/v1/jobs/take-b": [200, { id: "take-b", status: "succeeded" }],
  // Scenario 8: terminal SSE before the HTTP response.
  "POST /api/v1/shots/shot-3/frame": [202, { job_id: "batch-3", estimated_cost_usd: "0.0100" }],
  "/api/v1/jobs/batch-3": [200, {
    batch: { id: "batch-3", status: "succeeded" },
    children: [{ id: "take-x", status: "succeeded" }],
  }],
  // Scenario 9: POST answered only after a reconnect resync finished.
  "POST /api/v1/shots/shot-4/frame": [202, { job_id: "batch-4", estimated_cost_usd: "0.0100" }],
  "/api/v1/jobs/batch-4": [200, {
    batch: { id: "batch-4", status: "succeeded" },
    children: [{ id: "take-y", status: "succeeded" }],
  }],
  "/api/v1/jobs/take-y": [200, { id: "take-y", status: "succeeded" }],
  // Scenarios 11-14: retry / cancel.
  "POST /api/v1/jobs/r-1/retry": [202, { job_id: "r-1", estimated_cost_usd: "0.0100" }],
  "POST /api/v1/jobs/r-2/retry": [202, { job_id: "r-2", estimated_cost_usd: "0.0100" }],
  "POST /api/v1/jobs/r-bad/retry": [400, {
    code: "VALIDATION_ERROR", message: "Only failed jobs can be retried.", retryable: false,
  }],
  "POST /api/v1/jobs/c-1/cancel": [200, { job_id: "c-1" }],
  "/api/v1/jobs/c-1": [200, { id: "c-1", status: "running" }],
  "POST /api/v1/jobs/c-2/cancel": [400, {
    code: "VALIDATION_ERROR", message: "Only queued or running jobs can be canceled.", retryable: false,
  }],
  "/api/v1/jobs/c-2": [200, { id: "c-2", status: "succeeded" }],
  "POST /api/v1/jobs/c-3/cancel": [200, { job_id: "c-3" }],
  "/api/v1/jobs/c-3": [200, { id: "c-3", status: "canceled", error_code: "canceled" }],
  "/api/v1/shots/shot-5/references": [200, []],
  "/api/v1/shots/shot-5/continuity": [200, { available: false, step: "none", reason: "", summary: "" }],
  "/api/v1/jobs?shot_id=shot-5": [200, []],
  // Scenarios 15-20 (B10 review fixes).
  "/api/v1/jobs/h1": [200, { id: "h1", status: "failed", error_code: "PROVIDER_ERROR" }],
  "POST /api/v1/jobs/h1/retry": [202, { job_id: "h1", estimated_cost_usd: "0.0100" }],
  "POST /api/v1/jobs/m2/cancel": [200, { job_id: "m2" }],
  "/api/v1/jobs/m2": [200, { id: "m2", status: "canceled", error_code: "canceled" }],
  "POST /api/v1/jobs/m2b/cancel": [200, { job_id: "m2b" }],
  "/api/v1/jobs/m2b": [200, { id: "m2b", status: "running" }],
  "POST /api/v1/jobs/m3/retry": [400, {
    code: "VALIDATION_ERROR", message: "Only failed jobs can be retried.", retryable: false,
  }],
  "/api/v1/jobs/m3": [200, { id: "m3", status: "running" }],
  "POST /api/v1/jobs/m1/retry": [202, { job_id: "m1", estimated_cost_usd: "0.0100" }],
  "/api/v1/shots/shot-m1/references": [200, []],
  "/api/v1/shots/shot-m1/continuity": [200, { available: false, step: "none", reason: "", summary: "" }],
  "/api/v1/jobs?shot_id=shot-m1": [200, [{ id: "m1", job_type: "text_to_image", status: "queued", is_batch: false }]],
  "POST /api/v1/shots/shot-m4/frame": [202, { job_id: "m4-batch", estimated_cost_usd: "0.0100" }],
  "/api/v1/jobs/m4-batch": [200, {
    batch: { id: "m4-batch", status: "queued" }, children: [{ id: "m4-a", status: "queued" }],
  }],
  "/api/v1/shots/shot-m4/references": [200, []],
  "/api/v1/shots/shot-m4/continuity": [200, { available: false, step: "none", reason: "", summary: "" }],
  "/api/v1/jobs?shot_id=shot-m4": [200, [{ id: "m4-a", job_type: "text_to_image", status: "queued", is_batch: false }]],
  // Scenarios 21-22 (B10-r2 review fixes).
  "POST /api/v1/jobs/lo/retry": [202, { job_id: "lo", estimated_cost_usd: "0.0100" }],
  "/api/v1/jobs/lo": [200, { id: "lo", status: "running" }],
  "POST /api/v1/jobs/lo2/retry": [202, { job_id: "lo2", estimated_cost_usd: "0.0100" }],
  "/api/v1/jobs/lo2": [200, { id: "lo2", status: "running" }],
  "/api/v1/shots/shot-oo/references": [200, []],
  "/api/v1/shots/shot-oo/continuity": [200, { available: false, step: "none", reason: "", summary: "" }],
  "/api/v1/jobs?shot_id=shot-oo": [200, [{ id: "oo-1", job_type: "text_to_image", status: "queued", is_batch: false }]],
  // Scenario 24 (B10-r4 review fix).
  "POST /api/v1/jobs/vf/retry": [202, { job_id: "vf", estimated_cost_usd: "0.0100" }],
  "/api/v1/jobs/vf": [500, { code: "INTERNAL", message: "x", retryable: true }],
  "POST /api/v1/jobs/vf2/retry": [202, { job_id: "vf2", estimated_cost_usd: "0.0100" }],
  "/api/v1/jobs/vf2": [500, { code: "INTERNAL", message: "x", retryable: true }],
  // Scenario 23 (B10-r3 review fix).
  "POST /api/v1/jobs/sp/retry": [202, { job_id: "sp", estimated_cost_usd: "0.0100" }],
  "/api/v1/jobs/sp": [200, { id: "sp", status: "running" }],
  "POST /api/v1/jobs/sp2/retry": [202, { job_id: "sp2", estimated_cost_usd: "0.0100" }],
  "/api/v1/jobs/sp2": [200, { id: "sp2", status: "failed", error_code: "PROVIDER_ERROR" }],
  // Scenario 7: durable read of the OLD attempt, answered late.
  "/api/v1/jobs/late-1": [200, { id: "late-1", status: "failed", error_code: "PROVIDER_ERROR" }],
};
const requests = [];
const deferred = new Map(); // url -> release()
let deferUrl = null;
const deferSet = new Set(); // several requests held at once (scenario 15)
const holdSet = new Set(); // every request to these keys is queued in held[]
const held = []; // scenario 22: same URL held twice, released out of order
const server = http.createServer((req, res) => {
  const key = req.method === "GET" ? req.url : `${req.method} ${req.url}`;
  requests.push(key);
  const hit = ROUTES[key];
  const [code, body] = hit ?? [404, { code: "NOT_FOUND", message: "nf", retryable: false }];
  const send = () => {
    res.writeHead(code, { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" });
    res.end(JSON.stringify(body));
  };
  if (holdSet.has(key)) held.push(send);
  else if (deferUrl === key || deferSet.has(key)) deferred.set(key, send);
  else send();
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
process.env.NEXT_PUBLIC_API_URL = `http://127.0.0.1:${server.address().port}`;
globalThis.window = globalThis;

const failures = [];
const check = (cond, msg) => {
  if (!cond) failures.push(msg);
};

try {
  const workspaceMod = loadTs(path.join(WEB, "stores", "workspace.ts"));
  const { useWorkspaceStore } = workspaceMod;
  // Verification retries run on a fake timer: the test fires them by hand
  // and no real timer outlives the run (scenario 24).
  const verifyTimers = [];
  workspaceMod.setVerifyScheduler?.((fn, ms) => verifyTimers.push({ fn, ms }));
  const { useBoardStore } = loadTs(path.join(WEB, "stores", "board.ts"));
  const ws = useWorkspaceStore;

  const progress = (job_id, job_type) =>
    ws.getState().applySseEvent("job.progress", {
      job_id, job_type, status: "running", progress_pct: 40, stage: "생성 중",
    });
  progress("gen-done", "text_to_image");
  progress("gen-take2", "text_to_image");
  progress("batch-1", "text_to_image");
  progress("gen-running", "text_to_image");
  progress("gen-broken", "text_to_image");
  progress("tts-1", "tts");
  progress("tl-1", "render");
  // A job that already settled before the drop must not be re-read.
  ws.getState().applySseEvent("job.completed", {
    job_id: "gen-old", job_type: "text_to_image", status: "succeeded", output_count: 1,
  });

  useBoardStore.setState({ pendingTakes: { "shot-1": ["gen-done", "gen-take2"] } });
  check(
    useBoardStore.getState().takesSettled("shot-1", ws.getState().jobs) === false,
    "precondition: takes pending before resync"
  );

  const before = ws.getState();
  await ws.getState().resync();
  const after = ws.getState();
  const st = (id) => after.jobs[id]?.status;

  check(st("gen-done") === "completed", `gen-done -> ${st("gen-done")}`);
  check(st("gen-take2") === "failed", `gen-take2 -> ${st("gen-take2")}`);
  check(after.jobs["gen-take2"]?.error_code === "PROVIDER_ERROR", "failed job keeps error_code");
  check(st("batch-1") === "canceled", `batch-1 -> ${st("batch-1")}`);
  check(st("gen-running") === "running", `gen-running -> ${st("gen-running")}`);
  check(st("gen-broken") === "running", `gen-broken (read failed) -> ${st("gen-broken")}`);
  check(st("tts-1") === "completed", `tts-1 -> ${st("tts-1")}`);
  check(st("tl-1") === "completed", `tl-1 -> ${st("tl-1")}`);
  check(after.renders["tl-1"]?.status === "done", "render recorded for tl-1");
  check(after.eventSeq === before.eventSeq + 1, "eventSeq +1");
  check(after.resyncSeq === (before.resyncSeq ?? 0) + 1, "resyncSeq +1");
  check(
    useBoardStore.getState().takesSettled("shot-1", after.jobs) === true,
    "board takesSettled true after resync"
  );
  const expected = [
    "/api/v1/jobs/gen-done", "/api/v1/jobs/gen-take2", "/api/v1/jobs/batch-1",
    "/api/v1/jobs/gen-running", "/api/v1/jobs/gen-broken", "/api/v1/audio/tts-1",
    "/api/v1/timelines/tl-1",
  ];
  check(
    JSON.stringify([...requests].sort()) === JSON.stringify([...expected].sort()),
    `requests: ${JSON.stringify(requests)}`
  );
  console.log(`requests=${requests.length} jobs=${JSON.stringify(Object.fromEntries(
    Object.entries(after.jobs).map(([k, v]) => [k, v.status])))}`);

  // --- 6. HTTP-accepted takes with no SSE at all (B6R2-M1) -------------------
  requests.length = 0;
  await useBoardStore.getState().generateFrame("shot-2", { batchSize: 2 });
  const pending2 = useBoardStore.getState().pendingTakes["shot-2"];
  check(JSON.stringify(pending2) === JSON.stringify(["take-a", "take-b"]), `pending takes: ${JSON.stringify(pending2)}`);
  check(ws.getState().jobs["take-a"]?.status === "queued", "HTTP-accepted take tracked as queued");
  check(
    useBoardStore.getState().takesSettled("shot-2", ws.getState().jobs) === false,
    "precondition: shot-2 takes pending"
  );
  await ws.getState().resync();
  check(ws.getState().jobs["take-a"]?.status === "completed", `take-a -> ${ws.getState().jobs["take-a"]?.status}`);
  check(
    useBoardStore.getState().takesSettled("shot-2", ws.getState().jobs) === true,
    "shot-2 takesSettled after resync without any SSE"
  );
  // track() never downgrades a job whose terminal event already arrived.
  ws.getState().track([{ job_id: "take-a", job_type: "text_to_image" }]);
  check(ws.getState().jobs["take-a"]?.status === "completed", "track() keeps a settled job");

  // --- 7. late resync response vs newer SSE (B6R2-M2) ----------------------
  progress("late-1", "text_to_image");
  deferUrl = "/api/v1/jobs/late-1";
  const pendingResync = ws.getState().resync();
  for (let i = 0; i < 100 && !deferred.has(deferUrl); i++) {
    await new Promise((r) => setTimeout(r, 10));
  }
  check(deferred.has(deferUrl), "late-1 GET reached the server");
  // While the GET is in flight: a manual retry runs and completes.
  progress("late-1", "text_to_image");
  ws.getState().applySseEvent("job.completed", {
    job_id: "late-1", job_type: "text_to_image", status: "succeeded", output_count: 1,
  });
  deferred.get(deferUrl)?.();
  await pendingResync;
  deferUrl = null;
  check(
    ws.getState().jobs["late-1"]?.status === "completed",
    `late-1 after late failed response -> ${ws.getState().jobs["late-1"]?.status}`
  );

  // --- 8. terminal SSE first, HTTP pending registered later (B6R3-M1) --------
  ws.getState().applySseEvent("job.completed", {
    job_id: "take-x", job_type: "text_to_image", status: "succeeded", output_count: 1,
  });
  // Stand-in for the board screen's effect: runs whenever eventSeq moves.
  const settledSeen = [];
  let lastSeq = ws.getState().eventSeq;
  const unsubscribe = ws.subscribe((state) => {
    if (state.eventSeq === lastSeq) return;
    lastSeq = state.eventSeq;
    settledSeen.push(useBoardStore.getState().takesSettled("shot-3", state.jobs));
  });
  await useBoardStore.getState().generateFrame("shot-3", { batchSize: 1 });
  unsubscribe();
  check(ws.getState().jobs["take-x"]?.status === "completed", "take-x stays completed");
  check(
    settledSeen.includes(true),
    `settle effect saw the late pending take as settled (runs: ${JSON.stringify(settledSeen)})`
  );

  // --- 9. resync finished while the POST was in flight (B6R3-R2-M1) -------
  deferUrl = "POST /api/v1/shots/shot-4/frame";
  const posting = useBoardStore.getState().generateFrame("shot-4", { batchSize: 1 });
  for (let i = 0; i < 100 && !deferred.has(deferUrl); i++) {
    await new Promise((r) => setTimeout(r, 10));
  }
  check(deferred.has(deferUrl), "shot-4 POST reached the server");
  await ws.getState().resync(); // reconnect happens and completes meanwhile
  check(!ws.getState().jobs["take-y"], "precondition: resync could not know take-y");
  deferred.get(deferUrl)?.();
  deferUrl = null;
  await posting;
  for (let i = 0; i < 100 && ws.getState().jobs["take-y"]?.status !== "completed"; i++) {
    await new Promise((r) => setTimeout(r, 10));
  }
  check(
    ws.getState().jobs["take-y"]?.status === "completed",
    `take-y after a missed reconnect -> ${ws.getState().jobs["take-y"]?.status}`
  );
  check(
    useBoardStore.getState().takesSettled("shot-4", ws.getState().jobs) === true,
    "shot-4 takesSettled after the durable read"
  );

  // --- 10. canceled master / sheet jobs leave pending (B6R3-R2-M2) ---------
  const { useCharactersStore } = loadTs(path.join(WEB, "stores", "characters.ts"));
  const { useCharacterSheetStore } = loadTs(path.join(WEB, "stores", "character-sheet.ts"));
  useCharactersStore.setState({ masterJobByCharacter: { c1: "m-cancel", c2: "m-run" } });
  const masterDone = useCharactersStore.getState().settleMasterJobs({
    "m-cancel": { status: "canceled" },
    "m-run": { status: "running" },
  });
  check(
    JSON.stringify(masterDone.map((x) => [x.characterId, x.status])) === JSON.stringify([["c1", "canceled"]]),
    `master settled: ${JSON.stringify(masterDone)}`
  );
  check(
    JSON.stringify(useCharactersStore.getState().masterJobByCharacter) === JSON.stringify({ c2: "m-run" }),
    "canceled master job left pending"
  );
  useCharacterSheetStore.setState({
    pendingJobIds: ["s-cancel", "s-done", "s-run"], doneJobIds: [], failedJobIds: [],
    refresh: async () => {},
  });
  const sheetReported = useCharacterSheetStore.getState().settlePendingJobs({
    "s-cancel": { status: "canceled" },
    "s-done": { status: "completed" },
    "s-run": { status: "running" },
  });
  check(
    JSON.stringify(useCharacterSheetStore.getState().pendingJobIds) === JSON.stringify(["s-run"]),
    `sheet pending: ${JSON.stringify(useCharacterSheetStore.getState().pendingJobIds)}`
  );
  check(
    JSON.stringify(sheetReported.map((x) => x.status)) === JSON.stringify(["canceled"]),
    `sheet reported: ${JSON.stringify(sheetReported)}`
  );

  // --- 11. retry a failed take (C13) ----------------------------------------
  const { useToastStore } = loadTs(path.join(WEB, "stores", "toast.ts"));
  const waitFor = async (cond) => {
    for (let i = 0; i < 100 && !cond(); i++) await new Promise((r) => setTimeout(r, 10));
  };
  ws.getState().applySseEvent("job.failed", {
    job_id: "r-1", job_type: "text_to_image", error_code: "PROVIDER_ERROR",
    error_message: "boom", retryable: false, retry_count: 0, max_retries: 0,
  });
  useBoardStore.setState({ pendingTakes: {} });
  requests.length = 0;
  const retried = await useBoardStore.getState().retryTake("shot-5", { id: "r-1", job_type: "text_to_image" });
  check(retried === true, "retryTake resolves true on 202");
  check(requests.includes("POST /api/v1/jobs/r-1/retry"), `retry URL: ${JSON.stringify(requests)}`);
  check(ws.getState().jobs["r-1"]?.status === "queued", `r-1 after retry -> ${ws.getState().jobs["r-1"]?.status}`);
  check(
    JSON.stringify(useBoardStore.getState().pendingTakes["shot-5"]) === JSON.stringify(["r-1"]),
    `retried take pending: ${JSON.stringify(useBoardStore.getState().pendingTakes)}`
  );
  check(
    useBoardStore.getState().takesSettled("shot-5", ws.getState().jobs) === false,
    "retried take is not settled by its OLD failed state"
  );
  // The re-queued job is watched like a new one: a reconnect resync reads it.
  requests.length = 0;
  await ws.getState().resync();
  check(requests.includes("/api/v1/jobs/r-1"), `resync re-reads the retried job: ${JSON.stringify(requests)}`);
  ROUTES["/api/v1/jobs/r-1"] = [200, { id: "r-1", status: "succeeded" }];
  ws.getState().applySseEvent("job.completed", {
    job_id: "r-1", job_type: "text_to_image", status: "succeeded", output_count: 1,
  });
  // Inside the retry verify window the event is checked against the server
  // first (B10R3-M1), which by now has the take succeeded.
  await waitFor(() => useBoardStore.getState().takesSettled("shot-5", ws.getState().jobs));
  check(
    useBoardStore.getState().takesSettled("shot-5", ws.getState().jobs) === true,
    "retried take settles from its next SSE event"
  );
  // A rejected retry does not touch the job and toasts in Korean.
  ws.getState().applySseEvent("job.failed", {
    job_id: "r-bad", job_type: "text_to_image", error_code: "PROVIDER_ERROR",
    error_message: "boom", retryable: false, retry_count: 0, max_retries: 0,
  });
  const toastsBefore = useToastStore.getState().toasts.length;
  const rejected = await ws.getState().retryJob({ job_id: "r-bad", job_type: "text_to_image" });
  check(rejected === false, "retryJob resolves false on 400");
  check(ws.getState().jobs["r-bad"]?.status === "failed", "rejected retry keeps the job failed");
  const lastToast = useToastStore.getState().toasts.at(-1);
  check(
    useToastStore.getState().toasts.length === toastsBefore + 1 && lastToast?.kind === "error" &&
      lastToast.message.startsWith("실패한 작업만 재시도할 수 있습니다"),
    `rejected retry toast: ${JSON.stringify(lastToast)}`
  );

  // --- 12. SSE for the new attempt before the retry 202 (C13) ---------------
  ws.getState().applySseEvent("job.failed", {
    job_id: "r-2", job_type: "text_to_image", error_code: "PROVIDER_ERROR",
    error_message: "boom", retryable: false, retry_count: 0, max_retries: 0,
  });
  deferUrl = "POST /api/v1/jobs/r-2/retry";
  const retrying = ws.getState().retryJob({ job_id: "r-2", job_type: "text_to_image" });
  await waitFor(() => deferred.has(deferUrl));
  check(deferred.has(deferUrl), "r-2 retry POST reached the server");
  progress("r-2", "text_to_image"); // worker already picked the new attempt up
  deferred.get(deferUrl)?.();
  deferUrl = null;
  await retrying;
  check(
    ws.getState().jobs["r-2"]?.status === "running" && ws.getState().jobs["r-2"]?.progress_pct === 40,
    `r-2 keeps the newer SSE state: ${JSON.stringify(ws.getState().jobs["r-2"])}`
  );

  // --- 13. cancel is never optimistic (C13) ---------------------------------
  progress("c-1", "text_to_image");
  useBoardStore.setState({ pendingTakes: {} });
  deferUrl = "POST /api/v1/jobs/c-1/cancel";
  requests.length = 0;
  const canceling = useBoardStore.getState().cancelTake("shot-6", { id: "c-1", job_type: "text_to_image" });
  await waitFor(() => deferred.has(deferUrl));
  check(deferred.has(deferUrl), "c-1 cancel POST reached the server");
  check(ws.getState().jobs["c-1"]?.status === "running", "c-1 stays running while cancel is in flight");
  check(!useBoardStore.getState().pendingTakes["shot-6"], "nothing pending before the server answers");
  deferred.get(deferUrl)?.();
  deferUrl = null;
  check((await canceling) === true, "cancelTake resolves true on 200");
  await waitFor(() => requests.includes("/api/v1/jobs/c-1"));
  await new Promise((r) => setTimeout(r, 20));
  check(requests.includes("/api/v1/jobs/c-1"), `durable read after cancel: ${JSON.stringify(requests)}`);
  check(
    ws.getState().jobs["c-1"]?.status === "running",
    `c-1 not canceled while the server still says running -> ${ws.getState().jobs["c-1"]?.status}`
  );
  check(
    useBoardStore.getState().takesSettled("shot-6", ws.getState().jobs) === false,
    "canceled-requested take still pending until confirmed"
  );
  ws.getState().applySseEvent("job.canceled", { job_id: "c-1", job_type: "text_to_image" });
  check(ws.getState().jobs["c-1"]?.status === "canceled", "job.canceled settles c-1");
  check(
    useBoardStore.getState().takesSettled("shot-6", ws.getState().jobs) === true,
    "take settles once the server confirmed the cancel"
  );
  // Missed job.canceled: the durable read (server says canceled) settles it.
  progress("c-3", "text_to_image");
  await ws.getState().cancelJob({ job_id: "c-3", job_type: "text_to_image" });
  await waitFor(() => ws.getState().jobs["c-3"]?.status === "canceled");
  check(ws.getState().jobs["c-3"]?.status === "canceled", `c-3 via durable read -> ${ws.getState().jobs["c-3"]?.status}`);

  // --- 14. rejected cancel settles from the server, not canceled (C13) ------
  progress("c-2", "text_to_image");
  const toastsBeforeCancel = useToastStore.getState().toasts.length;
  const cancelRejected = await ws.getState().cancelJob({ job_id: "c-2", job_type: "text_to_image" });
  check(cancelRejected === false, "cancelJob resolves false on 400");
  const cancelToast = useToastStore.getState().toasts.at(-1);
  check(
    useToastStore.getState().toasts.length === toastsBeforeCancel + 1 && cancelToast?.kind === "error" &&
      cancelToast.message.startsWith("대기 중이거나 실행 중인 작업만 취소할 수 있습니다"),
    `rejected cancel toast: ${JSON.stringify(cancelToast)}`
  );
  await waitFor(() => ws.getState().jobs["c-2"]?.status === "completed");
  check(ws.getState().jobs["c-2"]?.status === "completed", `c-2 after rejected cancel -> ${ws.getState().jobs["c-2"]?.status}`);

  // --- 15. stale resync GET during the retry POST (B10-H1) -------------------
  progress("h1", "text_to_image"); // its job.failed was missed: local running
  const getH1 = "/api/v1/jobs/h1";
  const postH1 = "POST /api/v1/jobs/h1/retry";
  deferSet.add(getH1);
  deferSet.add(postH1);
  const resyncH1 = ws.getState().resync(); // starts reading the OLD attempt
  await waitFor(() => deferred.has(getH1));
  const retryH1 = ws.getState().retryJob({ job_id: "h1", job_type: "text_to_image" });
  await waitFor(() => deferred.has(postH1));
  check(deferred.has(getH1) && deferred.has(postH1), "h1 GET and retry POST both in flight");
  deferSet.clear();
  deferred.get(getH1)?.(); // old attempt's "failed" lands during the POST
  await resyncH1;
  check(ws.getState().jobs["h1"]?.status === "failed", "precondition: stale read applied during POST");
  ROUTES[getH1] = [200, { id: "h1", status: "running" }]; // server: new attempt
  deferred.get(postH1)?.();
  check((await retryH1) === true, "h1 retry accepted");
  await new Promise((r) => setTimeout(r, 30));
  check(
    ws.getState().jobs["h1"]?.status === "queued",
    `h1 after 202 despite the stale read -> ${ws.getState().jobs["h1"]?.status}`
  );
  requests.length = 0;
  await ws.getState().resync();
  check(requests.includes(getH1), `later resync re-reads h1: ${JSON.stringify(requests)}`);

  // --- 16. accepted cancel over an OLD terminal state (B10-M2) ---------------
  for (const id of ["m2", "m2b"]) {
    ws.getState().applySseEvent("job.failed", {
      job_id: id, job_type: "text_to_image", error_code: "PROVIDER_ERROR",
      error_message: "old attempt", retryable: false, retry_count: 0, max_retries: 0,
    });
  }
  requests.length = 0;
  check((await ws.getState().cancelJob({ job_id: "m2", job_type: "text_to_image" })) === true, "m2 cancel accepted");
  await waitFor(() => ws.getState().jobs["m2"]?.status === "canceled");
  check(requests.includes("/api/v1/jobs/m2"), `m2 re-read after accepted cancel: ${JSON.stringify(requests)}`);
  check(ws.getState().jobs["m2"]?.status === "canceled", `m2 (server canceled) -> ${ws.getState().jobs["m2"]?.status}`);
  check((await ws.getState().cancelJob({ job_id: "m2b", job_type: "text_to_image" })) === true, "m2b cancel accepted");
  await waitFor(() => ws.getState().jobs["m2b"]?.status === "running");
  check(
    ws.getState().jobs["m2b"]?.status === "running",
    `m2b (server still running) -> ${ws.getState().jobs["m2b"]?.status}, not failed/canceled`
  );

  // --- 17. retry rejected because the job is already active (B10-M3) --------
  ws.getState().applySseEvent("job.failed", {
    job_id: "m3", job_type: "text_to_image", error_code: "PROVIDER_ERROR",
    error_message: "old attempt", retryable: false, retry_count: 0, max_retries: 0,
  });
  requests.length = 0;
  check((await ws.getState().retryJob({ job_id: "m3", job_type: "text_to_image" })) === false, "m3 retry rejected");
  check(requests.includes("/api/v1/jobs/m3"), `m3 re-read after rejection: ${JSON.stringify(requests)}`);
  check(ws.getState().jobs["m3"]?.status === "running", `m3 after rejected retry -> ${ws.getState().jobs["m3"]?.status}`);

  // --- 18. header retry reloads the board card (B10-M1) ----------------------
  ws.getState().applySseEvent("job.failed", {
    job_id: "m1", job_type: "text_to_image", error_code: "PROVIDER_ERROR",
    error_message: "boom", retryable: false, retry_count: 0, max_retries: 0,
  });
  useBoardStore.setState({
    takes: { "shot-m1": [{ id: "m1", job_type: "text_to_image", status: "failed", is_batch: false }] },
    pendingTakes: {},
  });
  const sync = () => {
    const st = ws.getState();
    return useBoardStore.getState().syncTakesWithJobs(st.jobs, st.jobRev);
  };
  check(JSON.stringify(sync()) === "[]", "card agrees with the tracked failed job: no reload");
  requests.length = 0;
  await ws.getState().retryJob({ job_id: "m1", job_type: "text_to_image" }); // header path
  check(JSON.stringify(sync()) === JSON.stringify(["shot-m1"]), "header retry reloads shot-m1");
  await waitFor(() => useBoardStore.getState().takes["shot-m1"]?.[0]?.status === "queued");
  check(requests.includes("/api/v1/jobs?shot_id=shot-m1"), `shot-m1 takes re-read: ${JSON.stringify(requests)}`);
  check(useBoardStore.getState().takes["shot-m1"]?.[0]?.status === "queued", "card now shows the queued retry");
  check(JSON.stringify(sync()) === "[]", "no further reload without a job change");
  ROUTES["/api/v1/jobs/m1"] = [200, { id: "m1", status: "succeeded" }]; // committed before publish
  ws.getState().applySseEvent("job.completed", {
    job_id: "m1", job_type: "text_to_image", status: "succeeded", output_count: 1,
  });
  check(JSON.stringify(sync()) === JSON.stringify(["shot-m1"]), "completion reloads shot-m1 again");

  // --- 19. accepted generation reloads the open shot (B10-M4) ----------------
  useBoardStore.setState({ selectedShotId: "shot-m4", pendingTakes: {} });
  requests.length = 0;
  await useBoardStore.getState().generateFrame("shot-m4", { batchSize: 1 });
  await waitFor(() => (useBoardStore.getState().takes["shot-m4"] ?? []).length > 0);
  check(requests.includes("/api/v1/jobs?shot_id=shot-m4"), `open shot reloaded after generate: ${JSON.stringify(requests)}`);
  check(
    useBoardStore.getState().takes["shot-m4"]?.[0]?.status === "queued",
    "new queued take has a card (its 취소 is reachable) before the batch settles"
  );
  useBoardStore.setState({ selectedShotId: null });

  // --- 20. header rows tell same-type jobs apart (B10-M5) --------------------
  const { jobLabel } = loadTs(path.join(WEB, "lib", "jobs-api.ts"));
  const la = jobLabel({ job_id: "aaaaaaaa-1111", job_type: "text_to_image" });
  const lb = jobLabel({ job_id: "bbbbbbbb-2222", job_type: "text_to_image" });
  check(la !== lb && la === "text_to_image #aaaaaaaa", `labels: ${la} / ${lb}`);

  // --- 21. late OLD-attempt job.failed during the retry POST (B10R2-M1) ------
  const failedEvt = (job_id, msg) => ws.getState().applySseEvent("job.failed", {
    job_id, job_type: "text_to_image", error_code: "PROVIDER_ERROR",
    error_message: msg, retryable: false, retry_count: 0, max_retries: 0,
  });
  failedEvt("lo", "old attempt");
  deferUrl = "POST /api/v1/jobs/lo/retry";
  const retryLo = ws.getState().retryJob({ job_id: "lo", job_type: "text_to_image" });
  await waitFor(() => deferred.has(deferUrl));
  failedEvt("lo", "old attempt, published late"); // no attempt id in the payload
  deferred.get(deferUrl)?.();
  deferUrl = null;
  check((await retryLo) === true, "lo retry accepted");
  check(ws.getState().jobs["lo"]?.status === "queued", `lo right after 202 -> ${ws.getState().jobs["lo"]?.status}`);
  await waitFor(() => ws.getState().jobs["lo"]?.status === "running");
  check(ws.getState().jobs["lo"]?.status === "running", `lo after server check -> ${ws.getState().jobs["lo"]?.status}`);
  // Same race, but the new attempt's own SSE lands while the check is in flight.
  failedEvt("lo2", "old attempt");
  deferUrl = "POST /api/v1/jobs/lo2/retry";
  const retryLo2 = ws.getState().retryJob({ job_id: "lo2", job_type: "text_to_image" });
  await waitFor(() => deferred.has(deferUrl));
  failedEvt("lo2", "old attempt, published late");
  const lo2Get = "/api/v1/jobs/lo2";
  held.length = 0;
  holdSet.add(lo2Get); // hold the verification read(s)
  deferred.get(deferUrl)?.();
  deferUrl = null;
  await retryLo2;
  await waitFor(() => held.length === 1);
  check(held.length === 1, "lo2 verification read in flight");
  // The new attempt finishes: the server commits, then publishes the event
  // (which, inside the retry window, starts its own server check).
  ROUTES[lo2Get] = [200, { id: "lo2", status: "succeeded" }];
  ws.getState().applySseEvent("job.completed", {
    job_id: "lo2", job_type: "text_to_image", status: "succeeded", output_count: 1,
  });
  await waitFor(() => held.length === 2);
  holdSet.delete(lo2Get);
  held[0](); // the first check answers "running" — older than the event
  await new Promise((r) => setTimeout(r, 30));
  check(ws.getState().jobs["lo2"]?.status === "completed", `lo2 newer SSE wins -> ${ws.getState().jobs["lo2"]?.status}`);
  held[1]?.(); // the event's own check (absent before the B10-r4 window)
  await waitFor(() => !ws.getState().jobs["lo2"]?.verifying);
  check(
    ws.getState().jobs["lo2"]?.status === "completed" && !ws.getState().jobs["lo2"]?.verifying,
    `lo2 confirmed completed: ${JSON.stringify(ws.getState().jobs["lo2"])}`
  );
  held.length = 0;

  // --- 22. out-of-order shot-detail responses (B10R2-M2) ---------------------
  const ooKey = "/api/v1/jobs?shot_id=shot-oo";
  held.length = 0;
  holdSet.add(ooKey);
  const loadOld = useBoardStore.getState().loadShotDetail("shot-oo"); // server: queued
  await waitFor(() => held.length === 1);
  ROUTES[ooKey] = [200, [{ id: "oo-1", job_type: "text_to_image", status: "succeeded", is_batch: false }]];
  const loadNew = useBoardStore.getState().loadShotDetail("shot-oo"); // server: succeeded
  await waitFor(() => held.length === 2);
  check(held.length === 2, "two overlapping take loads in flight");
  holdSet.delete(ooKey);
  held[1](); // newer answers first
  await loadNew;
  held[0](); // older (queued) answers last
  await loadOld;
  check(
    useBoardStore.getState().takes["shot-oo"]?.[0]?.status === "succeeded",
    `card after out-of-order loads -> ${useBoardStore.getState().takes["shot-oo"]?.[0]?.status}`
  );

  // --- 23. old SSE split around the retry 202 (B10R3-M1) ---------------------
  const settled = (shot) => useBoardStore.getState().takesSettled(shot, ws.getState().jobs);
  failedEvt("sp", "old attempt");
  useBoardStore.setState({ pendingTakes: {} });
  deferUrl = "POST /api/v1/jobs/sp/retry";
  const retrySp = ws.getState().retryJob(
    { job_id: "sp", job_type: "text_to_image" },
    { onAccepted: () => useBoardStore.setState({ pendingTakes: { "shot-sp": ["sp"] } }) },
  );
  await waitFor(() => deferred.has(deferUrl));
  progress("sp", "text_to_image"); // OLD attempt's progress, published late
  deferred.get(deferUrl)?.();
  deferUrl = null;
  check((await retrySp) === true, "sp retry accepted");
  check(ws.getState().jobs["sp"]?.status === "running", "sp: active SSE during POST kept");
  failedEvt("sp", "old attempt, failure published after the 202");
  check(ws.getState().jobs["sp"]?.verifying === true, "sp: terminal event inside the window is provisional");
  check(settled("shot-sp") === false, "sp: pending take not settled by the unconfirmed failure");
  await waitFor(() => ws.getState().jobs["sp"]?.status === "running" && !ws.getState().jobs["sp"]?.verifying);
  check(
    ws.getState().jobs["sp"]?.status === "running" && !ws.getState().jobs["sp"]?.verifying,
    `sp after server check -> ${JSON.stringify(ws.getState().jobs["sp"])}`
  );
  check(settled("shot-sp") === false, "sp: take still pending (server says running)");
  // Genuine failure of the new attempt inside the window.
  failedEvt("sp2", "old attempt");
  await ws.getState().retryJob(
    { job_id: "sp2", job_type: "text_to_image" },
    { onAccepted: () => useBoardStore.setState({ pendingTakes: { "shot-sp2": ["sp2"] } }) },
  );
  check(ws.getState().jobs["sp2"]?.status === "queued", "sp2 queued after 202");
  failedEvt("sp2", "new attempt failed");
  check(settled("shot-sp2") === false, "sp2: not settled before the server confirms");
  await waitFor(() => !ws.getState().jobs["sp2"]?.verifying);
  check(
    ws.getState().jobs["sp2"]?.status === "failed" && !ws.getState().jobs["sp2"]?.verifying,
    `sp2 after server check -> ${JSON.stringify(ws.getState().jobs["sp2"])}`
  );
  check(settled("shot-sp2") === true, "sp2: settles once the server confirmed the failure");

  // --- 24. fail-closed terminal verification (B10R4-M1) ----------------------
  check(verifyTimers.length === 0, `no verification retries pending before 24: ${verifyTimers.length}`);
  const vfState = (id) => ws.getState().jobs[id];
  failedEvt("vf", "old attempt");
  await ws.getState().retryJob(
    { job_id: "vf", job_type: "text_to_image" },
    { onAccepted: () => useBoardStore.setState({ pendingTakes: { "shot-vf": ["vf"] } }) },
  );
  failedEvt("vf", "failure inside the window"); // server check answers 500
  await waitFor(() => verifyTimers.length === 1);
  check(
    vfState("vf")?.status === "failed" && vfState("vf")?.verifying === true,
    `vf after verify GET 500 -> ${JSON.stringify(vfState("vf"))}`
  );
  check(settled("shot-vf") === false, "vf: take not settled after a failed server check");
  check(verifyTimers[0]?.ms === 2000, `first retry after 2s: ${JSON.stringify(verifyTimers.map((t) => t.ms))}`);
  ROUTES["/api/v1/jobs/vf"] = [200, { id: "vf", status: "running" }]; // server recovers
  verifyTimers.shift()?.fn();
  await waitFor(() => vfState("vf")?.verifying === false);
  check(
    vfState("vf")?.status === "running" && vfState("vf")?.verifying === false,
    `vf after a successful retry -> ${JSON.stringify(vfState("vf"))}`
  );
  check(settled("shot-vf") === false, "vf: take still pending (server says running)");
  check(verifyTimers.length === 0, "vf: no more retries once verified");

  // Every retry fails: stays unsettled, one toast, a reconnect verifies it.
  failedEvt("vf2", "old attempt");
  await ws.getState().retryJob(
    { job_id: "vf2", job_type: "text_to_image" },
    { onAccepted: () => useBoardStore.setState({ pendingTakes: { "shot-vf2": ["vf2"] } }) },
  );
  const unconfirmed = () =>
    useToastStore.getState().toasts.filter((t) => t.message.includes("서버 상태를 확인하지 못했습니다")).length;
  const unconfirmedBefore = unconfirmed();
  failedEvt("vf2", "failure inside the window");
  const delays = [];
  for (let i = 0; i < 3; i++) {
    await waitFor(() => verifyTimers.length === 1);
    const t = verifyTimers.shift();
    if (!t) break;
    delays.push(t.ms);
    t.fn();
  }
  await waitFor(() => unconfirmed() > unconfirmedBefore);
  check(JSON.stringify(delays) === "[2000,5000,10000]", `retry delays: ${JSON.stringify(delays)}`);
  check(verifyTimers.length === 0, "vf2: retries bounded");
  check(unconfirmed() === unconfirmedBefore + 1, `vf2: one unconfirmed toast (${unconfirmed() - unconfirmedBefore})`);
  check(vfState("vf2")?.verifying === true, `vf2 still verifying -> ${JSON.stringify(vfState("vf2"))}`);
  check(settled("shot-vf2") === false, "vf2: take stays unsettled after all retries failed");

  // Scenario 25 (B10-r5 review fix): the retry window expires while the job is
  // still unverified; a later terminal SSE must stay unverified (fail-closed).
  const realNow = Date.now;
  Date.now = () => realNow() + 31_000;
  try {
    failedEvt("vf2", "terminal SSE after the window expired");
    check(
      vfState("vf2")?.verifying === true,
      `vf2 after window expiry -> ${JSON.stringify(vfState("vf2"))}`
    );
    check(settled("shot-vf2") === false, "vf2: still unsettled after window expiry");
    await waitFor(() => verifyTimers.length === 1);
    check(verifyTimers.length === 1, "vf2: expired-window event schedules a re-check");
  } finally {
    Date.now = realNow;
  }
  ROUTES["/api/v1/jobs/vf2"] = [200, { id: "vf2", status: "failed", error_code: "PROVIDER_ERROR" }];
  requests.length = 0;
  await ws.getState().resync();
  check(requests.includes("/api/v1/jobs/vf2"), `reconnect re-verifies vf2: ${JSON.stringify(requests)}`);
  check(
    vfState("vf2")?.status === "failed" && vfState("vf2")?.verifying === false,
    `vf2 after reconnect -> ${JSON.stringify(vfState("vf2"))}`
  );
  check(settled("shot-vf2") === true, "vf2: settles once the server confirmed");
  // The re-check scheduled in scenario 25 is now stale; running it must not undo the confirmation.
  verifyTimers.splice(0).forEach((t) => t.fn());
  await new Promise((r) => setTimeout(r, 20));
  check(
    vfState("vf2")?.status === "failed" && vfState("vf2")?.verifying === false,
    `vf2 after the stale re-check -> ${JSON.stringify(vfState("vf2"))}`
  );
} catch (e) {
  failures.push(`crash: ${e && e.stack ? e.stack : e}`);
} finally {
  server.close();
}

if (failures.length) {
  for (const f of failures) console.error(`FAIL: ${f}`);
  process.exit(1);
}
console.log("OK: resync re-reads durable job/render state and settles pending screens; retry re-tracks, cancel waits for the server");
