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
  // Scenario 7: durable read of the OLD attempt, answered late.
  "/api/v1/jobs/late-1": [200, { id: "late-1", status: "failed", error_code: "PROVIDER_ERROR" }],
};
const requests = [];
const deferred = new Map(); // url -> release()
let deferUrl = null;
const server = http.createServer((req, res) => {
  const key = req.method === "GET" ? req.url : `${req.method} ${req.url}`;
  requests.push(key);
  const hit = ROUTES[key];
  const [code, body] = hit ?? [404, { code: "NOT_FOUND", message: "nf", retryable: false }];
  const send = () => {
    res.writeHead(code, { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" });
    res.end(JSON.stringify(body));
  };
  if (deferUrl === key) deferred.set(key, send);
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
  const { useWorkspaceStore } = loadTs(path.join(WEB, "stores", "workspace.ts"));
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
} catch (e) {
  failures.push(`crash: ${e && e.stack ? e.stack : e}`);
} finally {
  server.close();
}

if (failures.length) {
  for (const f of failures) console.error(`FAIL: ${f}`);
  process.exit(1);
}
console.log("OK: resync re-reads durable job/render state and settles pending screens");
