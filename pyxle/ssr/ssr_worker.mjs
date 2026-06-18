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

// Main read loop: process requests from stdin serially.
async function main() {
  let buffer = '';

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

      // Handle cache invalidation messages.
      if (request.type === 'invalidate') {
        if (request.componentPath) {
          _bundleCache.delete(path.resolve(request.componentPath));
        } else {
          _bundleCache.clear();
        }
        process.stdout.write(JSON.stringify({ id, ok: true, invalidated: true }) + '\n');
        continue;
      }

      if (request.stream === true) {
        // Streaming render: emit framed NDJSON sharing the request id.
        const emit = (frame) => process.stdout.write(JSON.stringify({ id, ...frame }) + '\n');
        try {
          await renderRequestStream(request, emit);
        } catch (error) {
          // A failure before the first byte (component load / esbuild) is a
          // terminal error frame; the Python side falls back to a buffered
          // error render.
          emit({ type: 'error', error: String(error.message || error) });
        }
        continue;
      }

      try {
        const result = await renderRequest(request);
        const response = JSON.stringify({ id, ok: true, ...result });
        process.stdout.write(response + '\n');
      } catch (error) {
        const response = JSON.stringify({ id, ok: false, message: String(error.message || error) });
        process.stdout.write(response + '\n');
      }
    }
  }
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
async function resolveComponentBundle(resolvedPath, componentPath, workingDir, projectRoot, skipInlineCss) {
  const cached = _bundleCache.get(resolvedPath);
  if (cached) {
    return cached;
  }

  const registry = createStyleRegistry(projectRoot);
  const previousStyleHook = globalThis.__PYXLE_REGISTER_SSR_STYLE__;
  globalThis.__PYXLE_REGISTER_SSR_STYLE__ = (entry) => registry.register(entry);
  try {
    const { esbuild } = getProjectModules(projectRoot);
    const tempDir = getStableTempDir(projectRoot);
    const bundleHash = crypto.createHash('sha1').update(resolvedPath).digest('hex');
    const outfile = path.join(tempDir, `${bundleHash}.mjs`);

    await esbuild.build({
      entryPoints: [componentPath],
      bundle: true,
      format: 'esm',
      platform: 'node',
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
          },
        },
        {
          name: 'pyxle-inline-css',
          setup(build) {
            build.onLoad({ filter: /\.css$/ }, async (args) => {
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
  } finally {
    globalThis.__PYXLE_REGISTER_SSR_STYLE__ = previousStyleHook;
  }
}

/**
 * Resolve, compile (or load from the bundle cache), and instantiate the page
 * component for a render request. Installs the per-render SSR globals (style
 * and head registries, request pathname, CSRF token) and returns a
 * ``restoreGlobals`` closure the caller MUST invoke once rendering finishes so
 * the pathname/CSRF globals don't leak into the next request.
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
  const skipInlineCss = detectPostcssConfig(projectRoot) !== null;

  const headRegistry = createHeadRegistry();
  globalThis.__PYXLE_HEAD_REGISTRY__ = headRegistry;

  // Load the page bundle and replay its (isolated) style descriptors into this
  // render's registry.
  const pageBundle = await resolveComponentBundle(
    resolvedComponentPath, componentPath, workingDir, projectRoot, skipInlineCss,
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
      resolvedFallbackPath, fallbackPath, workingDir, projectRoot, skipInlineCss,
    );
    for (const descriptor of fallbackBundle.styleDescriptors) {
      styleRegistry.register(descriptor);
    }
    FallbackComponent =
      fallbackBundle.moduleExports.default ?? fallbackBundle.moduleExports.Component ?? null;
  }

  // Render-time style registration (rare) lands in this render's registry.
  globalThis.__PYXLE_REGISTER_SSR_STYLE__ = (entry) => styleRegistry.register(entry);

  const Component = pageBundle.moduleExports.default ?? pageBundle.moduleExports.Component;

  if (typeof Component !== 'function') {
    throw new Error('Component does not export a default function.');
  }

  // Make the request path / CSRF token visible to SSR code (e.g.
  // usePathname, <Form>'s hidden field). The returned ``restoreGlobals``
  // resets them so a later request without these values can't inherit the
  // previous request's value via the global.
  const previousPathname = globalThis.__PYXLE_CURRENT_PATHNAME__;
  const previousCsrf = globalThis.__PYXLE_CSRF_TOKEN__;
  if (typeof requestPathname === 'string') {
    globalThis.__PYXLE_CURRENT_PATHNAME__ = requestPathname;
  } else {
    delete globalThis.__PYXLE_CURRENT_PATHNAME__;
  }
  if (typeof csrfToken === 'string' && csrfToken.length > 0) {
    globalThis.__PYXLE_CSRF_TOKEN__ = csrfToken;
  } else {
    delete globalThis.__PYXLE_CSRF_TOKEN__;
  }

  const restoreGlobals = () => {
    if (previousPathname === undefined) {
      delete globalThis.__PYXLE_CURRENT_PATHNAME__;
    } else {
      globalThis.__PYXLE_CURRENT_PATHNAME__ = previousPathname;
    }
    if (previousCsrf === undefined) {
      delete globalThis.__PYXLE_CSRF_TOKEN__;
    } else {
      globalThis.__PYXLE_CSRF_TOKEN__ = previousCsrf;
    }
  };

  return {
    React,
    ReactDOMServer,
    Component,
    FallbackComponent,
    styleRegistry,
    headRegistry,
    restoreGlobals,
  };
}

/**
 * Buffered render: produce the complete HTML string in one shot via
 * ``renderToString``. This is the hot path for non-streaming, cacheable, and
 * statically generated pages — its behaviour is unchanged.
 */
async function renderRequest(request) {
  const { React, ReactDOMServer, Component, styleRegistry, headRegistry, restoreGlobals } =
    await loadComponentForRender(request);
  try {
    const element = React.createElement(Component, request.props);
    const html = ReactDOMServer.renderToString(element);
    const styles = styleRegistry.list();
    const headElements = headRegistry.list();
    return { html, styles, headElements };
  } finally {
    restoreGlobals();
  }
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
 */
async function renderRequestStream(request, emit) {
  const {
    React,
    ReactDOMServer,
    Component,
    FallbackComponent,
    styleRegistry,
    headRegistry,
    restoreGlobals,
  } = await loadComponentForRender(request);

  await new Promise((resolve) => {
    let settled = false;
    let piped = false;
    let shellReady = false;
    let renderer = null;
    let timer = null;

    const settle = (frame) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      restoreGlobals();
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
  });
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
