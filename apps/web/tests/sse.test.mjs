// SSE client contract test (G6 / C11).
//
//   node --experimental-eventsource tests/sse.test.mjs
//
// Runs the REAL apps/web/lib/sse.ts (and whatever it imports from lib/,
// transpiled with the installed `typescript` package) against a local
// 127.0.0.1 HTTP server that speaks the backend's wire format
// (`id:` / `event:` / `data:`, see services/api/app/routers/events.py).
// Only React is replaced, by a minimal hook harness (useState / useRef /
// useEffect). Node's native EventSource is the transport.
//
// Asserts:
//   1. every named event in packages/contracts/sse_events.json reaches the
//      onEvent callback with its name and JSON-parsed payload;
//   2. an unnamed (`message`) event still reaches onEvent;
//   3. after the server drops the stream, the client reconnects and calls
//      onResync exactly once (so screens refetch what they missed);
//   4. unmount closes the connection (no further events delivered).
// Exits non-zero on any failure.
import { createRequire } from "node:module";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB = path.resolve(HERE, "..");
const REPO = path.resolve(WEB, "..", "..");
const require = createRequire(import.meta.url);
const ts = require(path.join(WEB, "node_modules", "typescript"));

if (typeof globalThis.EventSource !== "function") {
  console.error("FAIL: no native EventSource — run with `node --experimental-eventsource`");
  process.exit(2);
}

const CONTRACT = JSON.parse(
  fs.readFileSync(path.join(REPO, "packages", "contracts", "sse_events.json"), "utf8")
);
const EVENT_NAMES = Object.keys(CONTRACT.events);

// --- minimal React hook harness ---------------------------------------------
const reactHarness = (() => {
  let current = null; // the mounted component instance during render
  function depsChanged(a, b) {
    if (!a || !b) return true;
    return a.length !== b.length || a.some((v, i) => !Object.is(v, b[i]));
  }
  function mount(hookFn) {
    const inst = { slots: [], effects: [], result: undefined, alive: true };
    const render = () => {
      if (!inst.alive) return;
      inst.idx = 0;
      current = inst;
      try {
        inst.result = hookFn();
      } finally {
        current = null;
      }
      // Commit: run effects whose deps changed (cleanup the previous run first).
      for (const eff of inst.effects) {
        if (eff.pending) {
          eff.pending = false;
          if (typeof eff.cleanup === "function") eff.cleanup();
          eff.cleanup = eff.fn();
        }
      }
    };
    inst.render = render;
    render();
    return {
      get result() {
        return inst.result;
      },
      unmount() {
        for (const eff of inst.effects) {
          if (typeof eff.cleanup === "function") eff.cleanup();
        }
        inst.alive = false;
      },
    };
  }
  function slot(init) {
    const inst = current;
    if (!inst) throw new Error("hook called outside the harness render");
    const i = inst.idx++;
    if (inst.slots.length <= i) inst.slots.push(init(inst));
    return { inst, i };
  }
  return {
    __mount: mount,
    useState(initial) {
      const { inst, i } = slot(() => ({
        value: typeof initial === "function" ? initial() : initial,
      }));
      const cell = inst.slots[i];
      const set = (next) => {
        const v = typeof next === "function" ? next(cell.value) : next;
        if (Object.is(v, cell.value)) return;
        cell.value = v;
        queueMicrotask(inst.render);
      };
      return [cell.value, set];
    },
    useRef(initial) {
      const { inst, i } = slot(() => ({ current: initial }));
      return inst.slots[i];
    },
    useEffect(fn, deps) {
      const { inst, i } = slot((owner) => ({ effectIndex: owner.effects.length }));
      const k = inst.slots[i].effectIndex;
      if (!inst.effects[k]) {
        inst.effects[k] = { fn, deps, pending: true, cleanup: undefined };
      } else {
        const eff = inst.effects[k];
        eff.fn = fn;
        if (depsChanged(eff.deps, deps)) {
          eff.deps = deps;
          eff.pending = true;
        }
      }
    },
  };
})();

// --- tiny CommonJS loader for lib/*.ts --------------------------------------
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
    if (spec === "react") return reactHarness;
    let target = null;
    if (spec.startsWith("@/")) target = path.join(WEB, spec.slice(2));
    else if (spec.startsWith(".")) target = path.resolve(path.dirname(file), spec);
    if (target) {
      for (const cand of [target, `${target}.ts`, `${target}.tsx`, path.join(target, "index.ts")]) {
        if (fs.existsSync(cand) && fs.statSync(cand).isFile()) return loadTs(cand);
      }
      throw new Error(`cannot resolve ${spec} from ${file}`);
    }
    return require(spec);
  };
  new Function("exports", "require", "module", "__filename", "__dirname", outputText)(
    mod.exports, localRequire, mod, file, path.dirname(file)
  );
  return mod.exports;
}

// --- local SSE server (backend wire format) ---------------------------------
let connections = 0;
const liveResponses = new Set();
let eventId = 0;
function frame(res, name, data) {
  eventId += 1;
  const head = name ? `event: ${name}\n` : "";
  res.write(`id: ${eventId}\n${head}data: ${JSON.stringify(data)}\n\n`);
}
const server = http.createServer((req, res) => {
  if (!req.url.startsWith("/api/v1/events")) {
    res.writeHead(404).end();
    return;
  }
  connections += 1;
  const n = connections;
  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "Access-Control-Allow-Origin": "*",
  });
  res.write("retry: 100\n\n");
  liveResponses.add(res);
  res.on("close", () => liveResponses.delete(res));
  if (n === 1) {
    for (const name of EVENT_NAMES) frame(res, name, { probe: name, conn: n });
    frame(res, null, { probe: "unnamed", conn: n });
    // Drop the stream so the client must reconnect (server restart, proxy cut).
    setTimeout(() => res.destroy(), 150);
  } else {
    frame(res, EVENT_NAMES[0], { probe: "after-reconnect", conn: n });
  }
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const { port } = server.address();
process.env.NEXT_PUBLIC_API_URL = `http://127.0.0.1:${port}`;
globalThis.window = globalThis; // sse.ts bails out when window is undefined (SSR)

// --- run the real hook --------------------------------------------------------
const { useSse } = loadTs(path.join(WEB, "lib", "sse.ts"));
const received = [];
const states = [];
let resyncs = 0;
const onEvent = (name, data) => received.push({ name, data });
const onResync = () => {
  resyncs += 1;
};
const handle = reactHarness.__mount(() => {
  const s = useSse(onEvent, { enabled: true, onResync });
  if (states[states.length - 1] !== s) states.push(s);
  return s;
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(pred, ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (pred()) return true;
    await sleep(20);
  }
  return pred();
}

const gotAfterReconnect = () => received.some((e) => e.data && e.data.probe === "after-reconnect");
await waitFor(() => gotAfterReconnect() && resyncs > 0, 5000);
await sleep(100);

const failures = [];
const namedFirst = received.filter((e) => e.data && e.data.conn === 1 && EVENT_NAMES.includes(e.name));
const namedSet = new Set(namedFirst.map((e) => e.name));
const missing = EVENT_NAMES.filter((n) => !namedSet.has(n));
console.log(`named_received=${namedSet.size}/${EVENT_NAMES.length}`);
if (missing.length) failures.push(`named events not delivered: ${missing.join(", ")}`);
for (const e of namedFirst) {
  if (!e.data || e.data.probe !== e.name) {
    failures.push(`payload of ${e.name} not JSON-parsed/matching: ${JSON.stringify(e.data)}`);
  }
}
const unnamed = received.some((e) => e.name === "message" && e.data && e.data.probe === "unnamed");
console.log(`unnamed_received=${unnamed ? 1 : 0}`);
if (!unnamed) failures.push("unnamed (message) event not delivered");
console.log(`connections=${connections} resyncs=${resyncs} after_reconnect=${gotAfterReconnect() ? 1 : 0}`);
console.log(`states=${states.join(">")}`);
if (connections < 2) failures.push("client did not reconnect after the stream dropped");
if (!gotAfterReconnect()) failures.push("event after reconnect not delivered");
if (resyncs !== 1) failures.push(`expected exactly 1 resync after reconnect, got ${resyncs}`);
if (!states.includes("connected")) failures.push("state never became 'connected'");

// Unmount closes the stream: nothing delivered afterwards.
const before = received.length;
handle.unmount();
await sleep(50);
for (const res of liveResponses) frame(res, EVENT_NAMES[0], { probe: "after-unmount" });
await sleep(150);
if (received.length !== before) failures.push("event delivered after unmount");
if (liveResponses.size !== 0) {
  await waitFor(() => liveResponses.size === 0, 1000);
  if (liveResponses.size !== 0) failures.push("connection still open after unmount");
}

server.close();
for (const res of liveResponses) res.destroy();
if (failures.length) {
  for (const f of failures) console.error(`FAIL: ${f}`);
  process.exit(1);
}
console.log("OK: sse.ts delivers all named events, resyncs after reconnect, closes on unmount");
process.exit(0);
