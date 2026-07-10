/**
 * Persistent Node.js SSR worker.
 *
 * Reads newline-delimited JSON render requests from stdin and writes
 * newline-delimited JSON responses to stdout. Keeps running until stdin
 * closes, eliminating per-request Node.js startup cost.
 *
 * Request format:
 *   {"id":"<uuid>","componentPath":"/abs/path","props":{},"clientRoot":"/abs","projectRoot":"/abs"}
 *
 * Response format (success):
 *   {"id":"<uuid>","ok":true,"html":"...","styles":[...],"headElements":[...]}
 *
 * Response format (error):
 *   {"id":"<uuid>","ok":false,"message":"..."}
 *
 * Streaming requests carry ``"stream":true`` and are answered with a sequence
 * of newline-delimited frames sharing the request id (consumed by the Python
 * worker pool's ``render_stream``):
 *   {"id":"<uuid>","type":"chunk","html":"..."}            -- a streamed slice
 *   {"id":"<uuid>","type":"end","styles":[...],"headElements":[...]}  -- success
 *   {"id":"<uuid>","type":"error","error":"..."}           -- terminal failure
 * The shell is only piped after ``onShellReady``, so a shell-level error maps
 * to a single terminal ``error`` frame and the buffered path stays the hot
 * path for non-streaming renders.
 */

import { AsyncLocalStorage } from 'node:async_hooks';
import { Console } from 'node:console';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { Writable } from 'node:stream';
import { pathToFileURL } from 'node:url';

// Pin the SSR worker's locale deterministically so ``toLocaleString()``,
// ``Intl.*``, and date/number formatting render the same way on every
// host the framework runs on. Without this, the worker inherits the
// machine's default locale (often en-US on CI, en-GB on EU servers,
// C on containers) and any server-rendered formatted date immediately
// mismatches what the browser produces, tripping React hydration. Apps
// that truly need locale-sensitive SSR can either (a) override
// ``PYXLE_SSR_LOCALE`` in their systemd/dotenv file, or (b) wrap the
// locale-sensitive render in ``<ClientOnly>``.
const _pyxleSsrLocale = process.env.PYXLE_SSR_LOCALE || 'en-US.UTF-8';
if (!process.env.LANG) process.env.LANG = _pyxleSsrLocale;
if (!process.env.LC_ALL) process.env.LC_ALL = _pyxleSsrLocale;

/**
 * Validate an environment-variable name as a safe JS identifier.
 *
 * Mirrors ``SAFE_IDENTIFIER_RE`` in ``pyxle/devserver/_security.py`` so the SSR
 * esbuild ``define`` accepts exactly the same ``PYXLE_PUBLIC_*`` keys the client
 * Vite ``define`` does — preventing ``import.meta.env.<bad name>`` from
 * injecting arbitrary text into the bundle.
 */
const _PYXLE_SAFE_IDENTIFIER_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/**
 * Build the esbuild ``define`` map for ``import.meta.env.PYXLE_PUBLIC_*``.
 *
 * Vite substitutes these public env vars into the *client* bundle, but the SSR
 * esbuild build did not — so during server render the expression evaluated to
 * ``undefined`` and any public env var rendered into the initial HTML mismatched
 * the hydrated client (blank first paint). Reading the same ``PYXLE_PUBLIC_*``
 * keys from ``process.env`` here keeps server and client output identical. The
 * Node worker inherits the dev server's environment, so the snapshot matches the
 * one Vite captured at startup. Each value is JSON-stringified because esbuild
 * treats ``define`` values as raw expressions (a bare string would be parsed as
 * an identifier).
 */
function buildPublicEnvDefine() {
  const define = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (!key.startsWith('PYXLE_PUBLIC_')) continue;
    if (!_PYXLE_SAFE_IDENTIFIER_RE.test(key)) continue;
    define[`import.meta.env.${key}`] = JSON.stringify(value ?? '');
  }
  return define;
}

// Snapshot once at worker startup — env vars are fixed for the process lifetime,
// matching the documented "restart to pick up a rotated PYXLE_PUBLIC_ var" rule.
const _pyxlePublicEnvDefine = buildPublicEnvDefine();

// Redirect all console output to stderr so it does not pollute the NDJSON protocol.
const stderrConsole = new Console({ stdout: process.stderr, stderr: process.stderr });
for (const method of ['log', 'info', 'warn', 'error', 'debug', 'dir', 'trace']) {
  if (typeof stderrConsole[method] === 'function') {
    console[method] = (...args) => stderrConsole[method](...args);
  }
}

/**
 * Per-request SSR context, propagated via ``AsyncLocalStorage``.
 *
 * The four ``__PYXLE_*`` names the render pipeline reads — the request
 * pathname, the CSRF token, the style-registration hook, and the head registry
 * — used to be plain globals that ``loadComponentForRender`` mutated per render
 * and restored afterwards. That approach forced streaming renders to run
 * one-at-a-time: a second request could not begin until the first restored the
 * globals, otherwise one user's CSRF token / head tags / styles could bleed
 * into another user's page while their streams interleaved.
 *
 * These names are now *configurable getters* that resolve against the current
 * async context's store. Each render runs inside ``ssrContext.run(context, …)``,
 * so React Suspense continuations, ``onShellReady``/``onAllReady`` callbacks,
 * and the streamed-chunk writes scheduled from within ``run`` all observe only
 * their own request's values. Reads made outside any render (no active store)
 * return ``undefined``, which keeps every existing ``typeof`` guard at the read
 * sites (in the browser bundle and ``render_component.mjs``) working unchanged.
 *
 * No code writes these globals any more, so the getters intentionally have no
 * setter — an accidental assignment would throw in strict mode, surfacing the
 * bug instead of silently reintroducing shared mutable state.
 */
const ssrContext = new AsyncLocalStorage();

const _SSR_CONTEXT_GLOBALS = [
  ['__PYXLE_CURRENT_PATHNAME__', 'pathname'],
  ['__PYXLE_CSRF_TOKEN__', 'csrfToken'],
  ['__PYXLE_REGISTER_SSR_STYLE__', 'registerStyle'],
  ['__PYXLE_HEAD_REGISTRY__', 'headRegistry'],
];
for (const [globalName, storeKey] of _SSR_CONTEXT_GLOBALS) {
  Object.defineProperty(globalThis, globalName, {
    configurable: true,
    get() {
      const store = ssrContext.getStore();
      return store ? store[storeKey] : undefined;
    },
  });
}

// Maximum number of render requests one worker processes concurrently.
//
// Buffered renders are synchronous (``renderToString``, a few ms) so they never
// really overlap, but a streaming render spends almost all of its wall-clock
// time IDLE — awaiting loader promises and Suspense boundaries. Blocking the
// stdin read loop on that idle time (``await handle(request)`` inline) serialised
// the whole site: with the default single worker, four concurrent streaming
// requests each waited for the previous stream to fully flush before their first
// byte. Handling requests concurrently — each isolated by ``ssrContext`` — lets
// interleaved streams progress independently. The cap bounds in-flight renders
// so a burst can't spawn unlimited concurrent work; override via the env var to
// tune for a workload with many slow, I/O-bound loaders.
const DEFAULT_WORKER_CONCURRENCY = 16;

function resolveWorkerConcurrency() {
  const raw = process.env.PYXLE_SSR_WORKER_CONCURRENCY;
  if (raw === undefined || raw === '') {
    return DEFAULT_WORKER_CONCURRENCY;
  }
  const parsed = Number(raw);
  if (Number.isInteger(parsed) && parsed > 0) {
    return parsed;
  }
  process.stderr.write(
    `SSR worker: ignoring invalid PYXLE_SSR_WORKER_CONCURRENCY=${raw}; ` +
      `using ${DEFAULT_WORKER_CONCURRENCY}\n`,
  );
  return DEFAULT_WORKER_CONCURRENCY;
}

const WORKER_CONCURRENCY = resolveWorkerConcurrency();

/**
 * Write one protocol frame to stdout as exactly one ``write()`` call.
 *
 * Node serialises a whole ``write()`` chunk on a pipe, so emitting each frame
 * with a single write keeps concurrent streams' bytes from interleaving inside
 * a frame (which would corrupt the NDJSON the Python read loop parses). Returns
 * the ``write()`` backpressure signal so a streaming render can pause its React
 * pipe until stdout drains.
 */
function writeFrame(frame) {
  return process.stdout.write(JSON.stringify(frame) + String.fromCharCode(10));
}

/**
 * Verify that a resolved path stays within the given boundary directory.
 *
 * Returns `true` when the resolved path is equal to or nested inside the
 * boundary.  Prevents path-traversal attacks via imports like
 * `/pages/../../../../etc/passwd`.
 */
function isPathWithinBoundary(resolved, boundary) {
  return resolved === boundary || resolved.startsWith(boundary + path.sep);
}

/**
 * Create a size-bounded LRU cache backed by a Map.
 *
 * Evicts the least-recently-used entry when the cache exceeds *maxSize*.
 * Prevents unbounded memory growth in long-running worker processes.
 */
function createLruCache(maxSize) {
  const map = new Map();
  return {
    has(key) { return map.has(key); },
    get(key) {
      const val = map.get(key);
      if (val !== undefined) {
        // Move to end (most-recently-used).
        map.delete(key);
        map.set(key, val);
      }
      return val;
    },
    set(key, val) {
      if (map.has(key)) map.delete(key);
      else if (map.size >= maxSize) {
        const oldest = map.keys().next().value;
        map.delete(oldest);
      }
      map.set(key, val);
    },
    delete(key) { map.delete(key); },
    clear() { map.clear(); },
    get size() { return map.size; },
  };
}

// Cache heavy modules per project root so they are loaded once, not per request.
// Bounded to 10 entries — one per project root; rarely more than 1 in practice.
const _moduleCache = createLruCache(10);

// Cache compiled component bundles so esbuild is only called once per component.
// Key: resolved componentPath, Value: { moduleExports, styleDescriptors }
// Bounded to 100 entries — a large app may have hundreds of pages but only a
// subset is rendered between invalidation cycles.
const _bundleCache = createLruCache(100);

// In-flight resolutions keyed by resolved component path. Now that renders run
// concurrently, two simultaneous cold requests for the SAME uncached component
// would otherwise both run esbuild against the same deterministic outfile and
// could read a torn bundle. The first caller compiles; the rest await its
// promise. Entries are cleared once the resolution settles.
const _bundleInFlight = new Map();

// Cache postcss.config.* presence per project root so we don't stat the
// filesystem on every render.  Bounded to 5 — effectively 1 per project.
const _postcssCache = createLruCache(5);

const POSTCSS_CONFIG_FILENAMES = [
  'postcss.config.cjs',
  'postcss.config.js',
  'postcss.config.mjs',
  'postcss.config.ts',
];

/**
 * Locate a PostCSS config file in the project root.
 *
 * When a PostCSS config is present, the project has opted into Vite's CSS
 * pipeline -- Vite (via PostCSS) compiles every imported stylesheet, hashes
 * it, and lists it in the manifest. Pyxle's build pipeline then writes the
 * hashed asset paths into ``page-manifest.json`` and the SSR template emits
 * a ``<link rel="stylesheet">`` tag on every render. The legacy
 * ``pyxle-inline-css`` esbuild plugin (which reads CSS files raw and dumps
 * them into a ``<style>`` block) becomes redundant in this mode -- worse,
 * it dumps unprocessed ``@tailwind`` directives that browsers can't parse
 * and duplicates payload that is already served via the hashed link.
 */
function detectPostcssConfig(projectRoot) {
  if (!projectRoot) return null;
  if (_postcssCache.has(projectRoot)) {
    return _postcssCache.get(projectRoot);
  }
  let result = null;
  for (const filename of POSTCSS_CONFIG_FILENAMES) {
    const candidate = path.join(projectRoot, filename);
    try {
      if (fs.statSync(candidate).isFile()) {
        result = candidate;
        break;
      }
    } catch {
      // File does not exist or is not accessible -- keep looking.
    }
  }
  _postcssCache.set(projectRoot, result);
  return result;
}

const _tailwindViteCache = createLruCache(5);

/**
 * Detect whether the project drives CSS through the ``@tailwindcss/vite``
 * plugin (Tailwind v4). When it does, Vite owns every stylesheet — inlining
 * the raw source here would dump an unresolved ``@import "tailwindcss"`` into a
 * ``<style>`` block — so the SSR runtime skips inlining, exactly as it does for
 * a PostCSS-configured project.
 */
function detectTailwindVite(projectRoot) {
  if (!projectRoot) return false;
  if (_tailwindViteCache.has(projectRoot)) {
    return _tailwindViteCache.get(projectRoot);
  }
  let result = false;
  try {
    const data = JSON.parse(
      fs.readFileSync(path.join(projectRoot, 'package.json'), 'utf8'),
    );
    for (const section of ['dependencies', 'devDependencies']) {
      const deps = data[section];
      if (deps && typeof deps === 'object' && '@tailwindcss/vite' in deps) {
        result = true;
        break;
      }
    }
  } catch {
    // No/invalid package.json -- assume no Tailwind plugin.
  }
  _tailwindViteCache.set(projectRoot, result);
  return result;
}

/**
 * Deterministic CSS Module class-name generator. MUST stay byte-for-byte
 * identical to ``CSS_MODULE_SCOPED_NAME_JS`` in
 * ``pyxle/devserver/client_files.py`` (Vite's ``css.modules.generateScopedName``)
 * so server- and client-rendered markup carry the same class names and React
 * hydration never mismatches. The name derives only from the file basename, the
 * local class name, and the stylesheet contents — never an absolute path — so
 * it is stable across dev, build, and production serve.
 */
function pyxleCssModuleClass(name, filename, css) {
  const file = String(filename).split(/[\\/]/).pop() || 'module';
  const base = file.replace(/\.module\.css$/i, '').replace(/[^a-zA-Z0-9_-]/g, '-');
  const seed = base + '|' + name + '|' + (css || '');
  let hash = 5381;
  for (let index = 0; index < seed.length; index += 1) {
    hash = ((hash << 5) + hash + seed.charCodeAt(index)) >>> 0;
  }
  return base + '_' + name + '_' + hash.toString(36).slice(0, 6);
}

/**
 * Read the project's import aliases from ``jsconfig.json`` so the SSR bundle
 * resolves ``@/…`` specifiers the same way Vite and the editor do. Returns
 * ``[{ prefix, dir }]`` where ``dir`` is the absolute directory the prefix maps
 * to (project root for the default ``@/* -> ./*``).
 */
function readProjectImportAliases(projectRoot) {
  if (!projectRoot) return [];
  try {
    const data = JSON.parse(
      fs.readFileSync(path.join(projectRoot, 'jsconfig.json'), 'utf8'),
    );
    const paths = data.compilerOptions && data.compilerOptions.paths;
    if (!paths || typeof paths !== 'object') return [];
    const out = [];
    for (const [key, value] of Object.entries(paths)) {
      if (!key.endsWith('/*') || !Array.isArray(value) || value.length === 0) continue;
      const prefix = key.slice(0, -2);
      if (!prefix || prefix.includes('/')) continue;
      let target = String(value[0]);
      target = target.endsWith('/*') ? target.slice(0, -2) : target;
      target = target.replace(/^\.\//, '') || '.';
      out.push({ prefix, dir: path.resolve(projectRoot, target) });
    }
    return out;
  } catch {
    return [];
  }
}

const _ALIAS_RESOLVE_EXTS = ['', '.jsx', '.js', '.tsx', '.ts', '.mjs', '.cjs', '.json'];
const _ALIAS_INDEX_EXTS = ['.jsx', '.js', '.tsx', '.ts', '.mjs'];

/** Resolve an aliased base path to a concrete file, probing extensions/index. */
function resolveAliasTarget(baseAbsPath) {
  for (const ext of _ALIAS_RESOLVE_EXTS) {
    const candidate = baseAbsPath + ext;
    try {
      if (fs.statSync(candidate).isFile()) return candidate;
    } catch {
      // keep probing
    }
  }
  for (const ext of _ALIAS_INDEX_EXTS) {
    const candidate = path.join(baseAbsPath, 'index' + ext);
    try {
      if (fs.statSync(candidate).isFile()) return candidate;
    } catch {
      // keep probing
    }
  }
  return null;
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

const _CSS_MODULE_CLASS_RE = /\.(-?[_a-zA-Z][\w-]*)/g;

/**
 * Build the ``local -> scoped`` class-name map for a ``*.module.css`` file,
 * matching the map Vite hands the client bundle.
 */
function buildCssModuleExports(css, filename) {
  const exportsMap = {};
  let match;
  _CSS_MODULE_CLASS_RE.lastIndex = 0;
  while ((match = _CSS_MODULE_CLASS_RE.exec(css)) !== null) {
    const local = match[1];
    if (!(local in exportsMap)) {
      exportsMap[local] = pyxleCssModuleClass(local, filename, css);
    }
  }
  return exportsMap;
}

// Stable temp directory per worker (created once, cleaned on exit).
let _stableTempDir = null;

function getStableTempDir(projectRoot) {
  if (_stableTempDir) return _stableTempDir;
  const baseDir = path.join(projectRoot, '.pyxle-build', '.ssr-tmp');
  fs.mkdirSync(baseDir, { recursive: true });
  _stableTempDir = fs.mkdtempSync(path.join(baseDir, 'worker-'));
  return _stableTempDir;
}

// Clean up temp dir on exit.
process.on('exit', () => {
  if (_stableTempDir) {
    try { fs.rmSync(_stableTempDir, { recursive: true, force: true }); } catch {}
  }
});

function getProjectModules(projectRoot) {
  if (_moduleCache.has(projectRoot)) {
    return _moduleCache.get(projectRoot);
  }
  const projectRequire = createProjectRequire(projectRoot);
  const modules = {
    esbuild: loadDependency('esbuild', projectRequire, projectRoot),
    React: loadDependency('react', projectRequire, projectRoot),
    ReactDOMServer: loadDependency('react-dom/server', projectRequire, projectRoot),
    projectRequire,
  };
  _moduleCache.set(projectRoot, modules);
  return modules;
}

/**
 * Handle a single render request end to end, writing its response frame(s).
 *
 * Buffered requests get one ``{ok}`` frame; streaming requests get a sequence
 * of ``chunk``/``end``/``error`` frames sharing the request id. A pre-first-byte
 * streaming failure (component load / esbuild) becomes one terminal ``error``
 * frame so the Python side can fall back to a buffered error render.
 */
async function handleRequest(request) {
  const { id } = request;
  if (request.stream === true) {
    const emit = (frame) => writeFrame({ id, ...frame });
    try {
      await renderRequestStream(request, emit);
    } catch (error) {
      emit({ type: 'error', error: String(error.message || error) });
    }
    return;
  }
  try {
    const result = await renderRequest(request);
    writeFrame({ id, ok: true, ...result });
  } catch (error) {
    writeFrame({ id, ok: false, message: String(error.message || error) });
  }
}

// Main read loop: parse stdin lines and dispatch renders concurrently.
//
// Requests are launched without awaiting them inline, so a slow streaming
// render never blocks the next request from starting. A bounded semaphore caps
// the number of in-flight renders: the loop only pauses reading once
// ``WORKER_CONCURRENCY`` renders are already running, then resumes as soon as
// one frees its slot. On stdin EOF, every in-flight render is awaited before the
// process exits so no response is truncated.
async function main() {
  let buffer = '';
  let inFlight = 0;
  const waiters = [];
  const pending = new Set();

  const acquireSlot = async () => {
    if (inFlight < WORKER_CONCURRENCY) {
      inFlight += 1;
      return;
    }
    // At capacity: block until a finishing render hands its slot to us. The
    // releaser resolves this promise WITHOUT decrementing ``inFlight`` (a direct
    // hand-off), so the count never dips and never exceeds the cap.
    await new Promise((resolve) => waiters.push(resolve));
  };

  const releaseSlot = () => {
    const next = waiters.shift();
    if (next) {
      next();
    } else {
      inFlight -= 1;
    }
  };

  const launch = (request) => {
    const task = (async () => {
      try {
        await handleRequest(request);
      } finally {
        releaseSlot();
      }
    })();
    pending.add(task);
    // Errors are already handled inside ``handleRequest``; this only reaps the
    // tracking entry once the render settles.
    task.finally(() => pending.delete(task));
  };

  for await (const chunk of process.stdin) {
    buffer += chunk.toString();
    let newlineIndex;
    while ((newlineIndex = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, newlineIndex).trim();
      buffer = buffer.slice(newlineIndex + 1);
      if (!line) {
        continue;
      }
      let request;
      try {
        request = JSON.parse(line);
      } catch {
        // Malformed JSON — cannot respond without an id, skip silently.
        process.stderr.write('SSR worker: malformed request line\n');
        continue;
      }
      const { id } = request;

      // Cache invalidation is synchronous and cheap; answer inline without
      // consuming a render slot so it can't be delayed behind slow streams.
      if (request.type === 'invalidate') {
        if (request.componentPath) {
          _bundleCache.delete(path.resolve(request.componentPath));
        } else {
          _bundleCache.clear();
        }
        writeFrame({ id, ok: true, invalidated: true });
        continue;
      }

      // Bounded concurrency: pause reading only when the cap is reached.
      await acquireSlot();
      launch(request);
    }
  }

  // stdin closed: let every in-flight render finish before exiting so its
  // response frames are fully written.
  await Promise.allSettled([...pending]);
  process.exit(0);
}

/**
 * Compile (or load from the bundle cache) a single component and return its
 * module exports plus the inline-style descriptors it registered.
 *
 * Each component is compiled with its OWN ephemeral style registry, so the
 * cached ``styleDescriptors`` belong to that component alone. This matters for
 * a ``loading.pyxl`` fallback, which is shared across every page it wraps — its
 * cache entry must never carry another page's styles. The caller replays the
 * returned descriptors into the active render's registry.
 */
async function resolveComponentBundle(resolvedPath, componentPath, workingDir, projectRoot) {
  const cached = _bundleCache.get(resolvedPath);
  if (cached) {
    return cached;
  }

  // Coalesce concurrent cold resolutions of the same component onto one compile.
  const pending = _bundleInFlight.get(resolvedPath);
  if (pending) {
    return pending;
  }

  // Whether Vite (not esbuild's inline-css plugin) owns CSS for this project.
  // Only the esbuild ``onLoad`` plugin below consumes it, and that runs solely
  // on this cache-miss compile — so it is detected here, per bundle build,
  // rather than on every warm-bundle render.
  const skipInlineCss =
    detectPostcssConfig(projectRoot) !== null || detectTailwindVite(projectRoot);

  const registry = createStyleRegistry(projectRoot);
  // The ``pyxle-inline-css`` plugin below emits module code that registers each
  // stylesheet by reading ``globalThis.__PYXLE_REGISTER_SSR_STYLE__`` as the
  // bundle is imported. Run the compile+import inside an ``ssrContext`` store
  // that points that hook at THIS component's own ephemeral registry, so
  // concurrent bundle resolutions never cross-register styles and the cached
  // ``styleDescriptors`` belong to this component alone.
  const resolution = ssrContext.run({ registerStyle: (entry) => registry.register(entry) }, async () => {
    const { esbuild } = getProjectModules(projectRoot);
    const tempDir = getStableTempDir(projectRoot);
    const bundleHash = crypto.createHash('sha1').update(resolvedPath).digest('hex');
    const outfile = path.join(tempDir, `${bundleHash}.mjs`);

    await esbuild.build({
      entryPoints: [componentPath],
      bundle: true,
      format: 'esm',
      platform: 'node',
      // Prefer a dependency's ESM build (``module`` field) over its CommonJS
      // ``main`` when it declares no ``exports`` map. Node's esbuild default is
      // ``['main', 'module']`` (CJS first); a CJS build that does
      // ``require('react')`` then breaks under our ESM output with a runtime
      // "Dynamic require of \"react\" is not supported", because React is
      // marked external. Preferring ESM (as Vite's SSR resolution does) makes
      // such packages — e.g. lucide-react and much of the shadcn/ui ecosystem —
      // resolve to an ``import``-based build that links cleanly against the
      // external React. Packages that ship an ``exports`` map are unaffected
      // (esbuild already picks the ``import`` condition for our ESM entry).
      mainFields: ['module', 'main'],
      outfile,
      jsx: 'automatic',
      sourcemap: false,
      logLevel: 'silent',
      absWorkingDir: workingDir,
      // Substitute import.meta.env.PYXLE_PUBLIC_* the same way Vite does for the
      // client bundle, so server-rendered HTML agrees with the hydrated client.
      define: _pyxlePublicEnvDefine,
      plugins: [
        {
          name: 'pyxle-pages-alias',
          setup(build) {
            build.onResolve({ filter: /^\/(pages|routes)\// }, (args) => {
              const resolved = path.resolve(workingDir, args.path.slice(1));
              if (!isPathWithinBoundary(resolved, workingDir)) {
                return { errors: [{ text: `Import path resolves outside the project: ${args.path}` }] };
              }
              return { path: resolved };
            });
            build.onResolve({ filter: /^pyxle\/client(?:\/.*)?$/ }, (args) => {
              const remainder = args.path.slice('pyxle/client'.length);
              const normalized =
                remainder === '' || remainder === '/' ? 'pyxle/client.js' : `pyxle${remainder}`;
              const resolved = path.resolve(workingDir, normalized);
              if (!isPathWithinBoundary(resolved, workingDir)) {
                return { errors: [{ text: `Import path resolves outside the project: ${args.path}` }] };
              }
              return { path: resolved };
            });

            // Resolve project import aliases (e.g. `@/components/ui/button`)
            // declared in jsconfig.json, mirroring the Vite client build so
            // shadcn/ui and other alias-based imports render on the server too.
            const importAliases = readProjectImportAliases(projectRoot);
            if (importAliases.length > 0) {
              const pattern = new RegExp(
                '^(' + importAliases.map((a) => escapeRegExp(a.prefix)).join('|') + ')/',
              );
              build.onResolve({ filter: pattern }, (args) => {
                for (const { prefix, dir } of importAliases) {
                  if (args.path !== prefix && !args.path.startsWith(prefix + '/')) {
                    continue;
                  }
                  const sub = args.path.slice(prefix.length).replace(/^\//, '');
                  const baseAbs = path.resolve(dir, sub);
                  if (!isPathWithinBoundary(baseAbs, projectRoot)) {
                    return {
                      errors: [
                        { text: `Import path resolves outside the project: ${args.path}` },
                      ],
                    };
                  }
                  const resolved = resolveAliasTarget(baseAbs);
                  if (!resolved) {
                    return { errors: [{ text: `Could not resolve "${args.path}"` }] };
                  }
                  return { path: resolved };
                }
                return null;
              });
            }
          },
        },
        {
          name: 'pyxle-inline-css',
          setup(build) {
            build.onLoad({ filter: /\.css$/ }, async (args) => {
              if (/\.module\.css$/i.test(args.path)) {
                // CSS Module: export the local -> scoped class-name map so
                // `styles.foo` resolves to the exact class Vite emits on the
                // client (no hydration mismatch). The stylesheet's own rules
                // are compiled and delivered by Vite via the manifest link.
                const moduleCss = await fs.promises.readFile(args.path, 'utf8');
                const exportsMap = buildCssModuleExports(moduleCss, args.path);
                return {
                  contents: `export default ${JSON.stringify(exportsMap)};`,
                  loader: 'js',
                  resolveDir: path.dirname(args.path),
                };
              }
              if (skipInlineCss) {
                // Project has postcss.config.* -- Vite owns CSS via the
                // manifest pipeline. Reading and inlining the raw source
                // here would dump unparseable @tailwind directives and
                // duplicate the hashed <link> the SSR template already
                // emits. Resolve to an empty side-effect module instead.
                return {
                  contents: 'export default "";',
                  loader: 'js',
                  resolveDir: path.dirname(args.path),
                };
              }
              const contents = await fs.promises.readFile(args.path, 'utf8');
              const descriptor = registry.describe(args.path, contents);
              const moduleCode = `const entry = ${JSON.stringify(descriptor)};
if (typeof globalThis.__PYXLE_REGISTER_SSR_STYLE__ === 'function') {
  globalThis.__PYXLE_REGISTER_SSR_STYLE__(entry);
}
export default entry.contents;
`;
              return { contents: moduleCode, loader: 'js', resolveDir: path.dirname(args.path) };
            });
          },
        },
      ],
      external: [
        'react',
        'react-dom',
        'react-dom/server',
        'react/jsx-runtime',
        'react/jsx-dev-runtime',
      ],
    });

    // esbuild rewrites this fixed-name outfile (hashed from the entry PATH, not
    // its content) on every rebuild, but Node's ESM loader caches modules by URL.
    // Re-importing the same URL after a hot-reload rebuild therefore returns the
    // STALE module — the cached compile, not the file esbuild just rewrote. Bust
    // the cache with a hash of the bundled output so a changed bundle is always
    // re-imported, while an unchanged one still reuses Node's module cache.
    const bundledSource = await fs.promises.readFile(outfile, 'utf8');
    const cacheBuster = crypto.createHash('sha1').update(bundledSource).digest('hex');
    const moduleUrl = `${pathToFileURL(outfile).href}?v=${cacheBuster}`;
    const moduleExports = await import(moduleUrl);

    const entry = { moduleExports, styleDescriptors: registry.list() };
    _bundleCache.set(resolvedPath, entry);
    return entry;
  });

  // Register before the first await so a concurrent caller coalesces; clear on
  // settle (success populates ``_bundleCache``; failure must not pin a rejected
  // promise so the next request can retry).
  _bundleInFlight.set(resolvedPath, resolution);
  try {
    return await resolution;
  } finally {
    _bundleInFlight.delete(resolvedPath);
  }
}

/**
 * Resolve, compile (or load from the bundle cache), and instantiate the page
 * component for a render request. Builds the per-request SSR *context* (style
 * and head registries, request pathname, CSRF token, and the render-time
 * style-registration hook) and returns it alongside the component.
 *
 * The caller runs the actual React render inside ``ssrContext.run(context, …)``
 * so every read of the ``__PYXLE_*`` globals — including from React Suspense
 * continuations of concurrently-interleaved streams — resolves against this
 * request's context and never another's. Nothing is mutated globally, so there
 * is no per-request teardown to remember.
 *
 * When the request carries a ``fallbackPath`` (a compiled ``loading.pyxl``),
 * its component is loaded too and returned as ``FallbackComponent`` so the
 * streaming render can wrap the page in ``<Suspense fallback={<Loading/>}>``.
 *
 * Shared by the buffered (``renderRequest``) and streaming
 * (``renderRequestStream``) paths so both compile and cache bundles
 * identically.
 */
async function loadComponentForRender({
  componentPath,
  clientRoot,
  projectRoot: projectRootArg,
  requestPathname,
  csrfToken,
  fallbackPath,
}) {
  if (!componentPath) {
    throw new Error('Missing componentPath in render request.');
  }

  const resolvedComponentPath = path.resolve(componentPath);
  const workingDir = clientRoot ? path.resolve(clientRoot) : path.dirname(componentPath);
  const projectRoot = resolveProjectRoot(projectRootArg, workingDir, componentPath);
  if (!projectRoot) {
    throw new Error('Unable to determine project root for SSR render.');
  }

  const { React, ReactDOMServer } = getProjectModules(projectRoot);

  // Fresh registries for each render (head/style depend on props/render).
  const styleRegistry = createStyleRegistry(projectRoot);

  const headRegistry = createHeadRegistry();

  // Load the page bundle and replay its (isolated) style descriptors into this
  // render's registry.
  const pageBundle = await resolveComponentBundle(
    resolvedComponentPath, componentPath, workingDir, projectRoot,
  );
  for (const descriptor of pageBundle.styleDescriptors) {
    styleRegistry.register(descriptor);
  }

  // Optional loading.pyxl fallback (route-level <Suspense> shell). Loaded the
  // same way — its own isolated descriptors are merged into this render so the
  // fallback's styles still ship.
  let FallbackComponent = null;
  if (fallbackPath) {
    const resolvedFallbackPath = path.resolve(fallbackPath);
    const fallbackBundle = await resolveComponentBundle(
      resolvedFallbackPath, fallbackPath, workingDir, projectRoot,
    );
    for (const descriptor of fallbackBundle.styleDescriptors) {
      styleRegistry.register(descriptor);
    }
    FallbackComponent =
      fallbackBundle.moduleExports.default ?? fallbackBundle.moduleExports.Component ?? null;
  }

  const Component = pageBundle.moduleExports.default ?? pageBundle.moduleExports.Component;

  if (typeof Component !== 'function') {
    throw new Error('Component does not export a default function.');
  }

  // The per-request context backing the ``__PYXLE_*`` getters while this render
  // runs. Absent values are ``undefined`` so the getters report ``undefined``
  // (matching the old "delete the global" behaviour) and the ``typeof`` guards
  // at the read sites skip cleanly — e.g. a request without a CSRF token can't
  // inherit another request's token.
  const context = {
    pathname: typeof requestPathname === 'string' ? requestPathname : undefined,
    csrfToken: typeof csrfToken === 'string' && csrfToken.length > 0 ? csrfToken : undefined,
    styleRegistry,
    headRegistry,
    // Render-time style registration (rare) lands in this render's registry.
    registerStyle: (entry) => styleRegistry.register(entry),
  };

  return {
    React,
    ReactDOMServer,
    Component,
    FallbackComponent,
    styleRegistry,
    headRegistry,
    context,
  };
}

/**
 * Buffered render: produce the complete HTML string in one shot via
 * ``renderToString``. This is the hot path for non-streaming, cacheable, and
 * statically generated pages — its behaviour is unchanged. The synchronous
 * render runs inside ``ssrContext.run`` so its ``__PYXLE_*`` reads resolve
 * against this request's context.
 */
async function renderRequest(request) {
  const { React, ReactDOMServer, Component, styleRegistry, headRegistry, context } =
    await loadComponentForRender(request);
  return ssrContext.run(context, () => {
    const element = React.createElement(Component, request.props);
    const html = ReactDOMServer.renderToString(element);
    return { html, styles: styleRegistry.list(), headElements: headRegistry.list() };
  });
}

/**
 * Streaming render: drive React's ``renderToPipeableStream`` and call
 * ``emit(frame)`` for each protocol frame (``chunk`` / ``end`` / ``error``).
 *
 * The shell is piped only after ``onShellReady`` so a shell-level failure
 * surfaces as a single terminal ``error`` frame (the Python side then renders
 * a buffered error page, preserving the error-boundary contract). Backpressure
 * propagates end-to-end: when stdout can't accept more, the pipe pauses until
 * it drains. A render that never completes (e.g. a Suspense boundary that
 * hangs) is aborted after ``streamTimeout`` ms so the worker is never wedged.
 *
 * The whole render is driven inside ``ssrContext.run(context, …)``:
 * ``renderToPipeableStream`` is invoked synchronously within ``run``, so React
 * captures this request's async context and every Suspense continuation,
 * ``onShellReady`` callback, and streamed-chunk write it later schedules reads
 * the correct request's pathname / CSRF token / head registry — never a
 * concurrently-interleaved request's.
 */
async function renderRequestStream(request, emit) {
  const {
    React,
    ReactDOMServer,
    Component,
    FallbackComponent,
    styleRegistry,
    headRegistry,
    context,
  } = await loadComponentForRender(request);

  await ssrContext.run(context, () => new Promise((resolve) => {
    let settled = false;
    let piped = false;
    let shellReady = false;
    let renderer = null;
    let timer = null;

    const settle = (frame) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      if (frame) emit(frame);
      resolve();
    };

    const collector = new Writable({
      write(chunk, _encoding, callback) {
        // Respect stdout backpressure: pause the React pipe until the OS pipe
        // (drained by the Python read loop) can accept more output.
        const ok = emit({ type: 'chunk', html: chunk.toString('utf8') });
        if (ok) {
          callback();
        } else {
          process.stdout.once('drain', callback);
        }
      },
    });
    collector.on('finish', () =>
      settle({ type: 'end', styles: styleRegistry.list(), headElements: headRegistry.list() }),
    );
    collector.on('error', (err) =>
      settle({ type: 'error', error: String((err && err.message) || err) }),
    );

    const tryPipe = () => {
      if (settled || piped || !shellReady || !renderer) return;
      piped = true;
      renderer.pipe(collector);
    };

    try {
      // A loading.pyxl boundary wraps the page in <Suspense fallback={<Loading/>}>
      // so the loading state streams as the shell while the page render
      // suspends. The client hydration entry wraps identically (driven by the
      // same per-route descriptor), so the boundary structure matches.
      const pageElement = React.createElement(Component, request.props);
      const element = FallbackComponent
        ? React.createElement(
            React.Suspense,
            { fallback: React.createElement(FallbackComponent) },
            pageElement,
          )
        : pageElement;
      renderer = ReactDOMServer.renderToPipeableStream(element, {
        onShellReady() {
          shellReady = true;
          tryPipe();
        },
        onShellError(error) {
          settle({ type: 'error', error: String((error && error.message) || error) });
        },
        onError(error) {
          // Recoverable error inside a Suspense boundary after the shell
          // flushed: React streams the fallback and retries on the client.
          // Log it; don't break the NDJSON protocol.
          process.stderr.write(`SSR stream error: ${String((error && error.message) || error)}\n`);
        },
      });
    } catch (error) {
      settle({ type: 'error', error: String((error && error.message) || error) });
      return;
    }

    // ``onShellReady`` may already have fired synchronously above; pipe now
    // that ``renderer`` is assigned.
    tryPipe();

    const timeoutMs = Number(request.streamTimeout) > 0 ? Number(request.streamTimeout) : 30000;
    timer = setTimeout(() => {
      try {
        renderer.abort();
      } catch {
        // Already finished — nothing to abort.
      }
    }, timeoutMs);
    if (typeof timer.unref === 'function') timer.unref();
  }));
}

// --- Helpers (shared with render_component.mjs) ---

function resolveProjectRoot(projectRootArg, workingDir, componentPath) {
  if (projectRootArg && projectRootArg !== 'undefined') {
    return path.resolve(projectRootArg);
  }
  const inferredFromClient = workingDir ? path.resolve(workingDir, '..', '..') : null;
  if (inferredFromClient && fs.existsSync(inferredFromClient)) {
    return inferredFromClient;
  }
  let current = path.dirname(componentPath);
  while (current && current !== path.dirname(current)) {
    if (
      path.basename(current) === 'client' &&
      path.basename(path.dirname(current)) === '.pyxle-build'
    ) {
      return path.dirname(path.dirname(current));
    }
    current = path.dirname(current);
  }
  return null;
}

function createProjectRequire(projectRoot) {
  if (!projectRoot) {
    return null;
  }
  const virtualEntry = path.join(projectRoot, 'pyxle-ssr-runtime.js');
  return createRequire(virtualEntry);
}

function createProjectTempDir(projectRoot) {
  const baseDir = path.join(projectRoot, '.pyxle-build', '.ssr-tmp');
  fs.mkdirSync(baseDir, { recursive: true });
  return fs.mkdtempSync(path.join(baseDir, 'run-'));
}

function loadDependency(specifier, projectRequire, projectRoot) {
  const loaders = [];
  if (projectRequire) {
    loaders.push(projectRequire);
  }
  loaders.push(createRequire(import.meta.url));

  let lastError;
  for (const loader of loaders) {
    try {
      return loader(specifier);
    } catch (error) {
      lastError = error;
    }
  }
  const location = projectRoot ? ` from '${projectRoot}'` : '';
  throw new Error(
    `Unable to resolve '${specifier}'${location}. Run 'npm install ${specifier}' in your project.`,
    { cause: lastError },
  );
}

function createStyleRegistry(projectRoot) {
  const map = new Map();
  return {
    register(entry) {
      if (!entry || typeof entry !== 'object') {
        return;
      }
      const { identifier } = entry;
      if (typeof identifier !== 'string' || map.has(identifier)) {
        return;
      }
      map.set(identifier, entry);
    },
    describe(filePath, contents) {
      const source = normalizeStyleSource(filePath, projectRoot);
      return { identifier: makeStyleIdentifier(source), source, contents };
    },
    list() {
      return Array.from(map.values());
    },
  };
}

function normalizeStyleSource(filePath, projectRoot) {
  const absolute = path.resolve(filePath);
  if (projectRoot) {
    const relative = path.relative(projectRoot, absolute);
    if (!relative.startsWith('..') && !path.isAbsolute(relative)) {
      return relative.split(path.sep).join('/');
    }
  }
  return path.basename(filePath);
}

function makeStyleIdentifier(source) {
  const base = typeof source === 'string' && source ? source : 'style';
  const digest = crypto.createHash('sha1').update(base).digest('hex').slice(0, 12);
  const safe = base.replace(/[^a-zA-Z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'style';
  return `pyxle-inline-style-${safe}-${digest}`;
}

function createHeadRegistry() {
  const elements = [];
  return {
    register(element) {
      if (!element || typeof element !== 'string') {
        return;
      }
      elements.push(element);
    },
    list() {
      return elements;
    },
  };
}

main().catch((error) => {
  process.stderr.write(`SSR worker fatal error: ${error.message}\n`);
  process.exit(1);
});
