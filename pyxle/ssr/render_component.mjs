import { Console } from 'node:console';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

// Pin the SSR worker's locale deterministically so Intl/Date formatting
// produces the same strings the browser will produce on hydration. See
// ssr_worker.mjs for the rationale.
const _pyxleSsrLocale = process.env.PYXLE_SSR_LOCALE || 'en-US.UTF-8';
if (!process.env.LANG) process.env.LANG = _pyxleSsrLocale;
if (!process.env.LC_ALL) process.env.LC_ALL = _pyxleSsrLocale;

// Mirrors SAFE_IDENTIFIER_RE in pyxle/devserver/_security.py so the SSR esbuild
// define accepts exactly the PYXLE_PUBLIC_* keys the client Vite define does.
const _PYXLE_SAFE_IDENTIFIER_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/**
 * Build the esbuild ``define`` map for ``import.meta.env.PYXLE_PUBLIC_*``.
 *
 * Vite substitutes these public env vars into the *client* bundle; without a
 * matching define here the same expression renders as ``undefined`` on the
 * server, so any public env var baked into the initial HTML mismatches the
 * hydrated client (blank first paint). Reading the same keys from
 * ``process.env`` keeps server and client output identical. Values are
 * JSON-stringified because esbuild treats ``define`` values as raw expressions.
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

const REACT_EXTERNALS = [
  'react',
  'react-dom',
  'react-dom/server',
  'react/jsx-runtime',
  'react/jsx-dev-runtime',
];

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
  for (const filename of POSTCSS_CONFIG_FILENAMES) {
    const candidate = path.join(projectRoot, filename);
    try {
      if (fs.statSync(candidate).isFile()) return candidate;
    } catch {
      // File does not exist or is not accessible -- keep looking.
    }
  }
  return null;
}

/**
 * Detect whether the project drives CSS through the ``@tailwindcss/vite``
 * plugin (Tailwind v4). When it does, Vite owns every stylesheet — inlining
 * the raw source here would dump an unresolved ``@import "tailwindcss"`` into a
 * ``<style>`` block — so the SSR runtime skips inlining, exactly as it does for
 * a PostCSS-configured project.
 */
function detectTailwindVite(projectRoot) {
  if (!projectRoot) return false;
  try {
    const data = JSON.parse(
      fs.readFileSync(path.join(projectRoot, 'package.json'), 'utf8'),
    );
    for (const section of ['dependencies', 'devDependencies']) {
      const deps = data[section];
      if (deps && typeof deps === 'object' && '@tailwindcss/vite' in deps) {
        return true;
      }
    }
  } catch {
    // No/invalid package.json -- assume no Tailwind plugin.
  }
  return false;
}

/**
 * Deterministic CSS Module class-name generator. MUST stay byte-for-byte
 * identical to ``CSS_MODULE_SCOPED_NAME_JS`` in
 * ``pyxle/devserver/client_files.py`` (used as Vite's
 * ``css.modules.generateScopedName``) so server- and client-rendered markup
 * carry the same class names and React hydration never mismatches. The name
 * derives only from the file basename, the local class name, and the
 * stylesheet contents — never an absolute path — so it is stable across dev,
 * build, and production serve.
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
 * to (project root for the default ``@/* -> ./*``). Malformed configs yield an
 * empty list — the page simply has no path aliases.
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
 * matching the map Vite hands the client bundle. Used as the default export of
 * a CSS-module import during SSR so ``styles.foo`` resolves to the same class
 * name the browser renders.
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

async function render() {
  const [, , componentPath, propsJson, clientRoot, projectRootArg] = process.argv;

  if (!componentPath) {
    throw new Error('Missing component path argument.');
  }

  redirectConsoleToStderr();

  const props = propsJson ? JSON.parse(propsJson) : {};
  const workingDir = clientRoot ? path.resolve(clientRoot) : path.dirname(componentPath);
  const projectRoot = resolveProjectRoot(projectRootArg, workingDir, componentPath);
  if (!projectRoot) {
    throw new Error('Unable to determine project root for SSR runtime.');
  }
  const styleRegistry = createStyleRegistry(projectRoot);
  globalThis.__PYXLE_REGISTER_SSR_STYLE__ = (entry) => styleRegistry.register(entry);
  const skipInlineCss =
    detectPostcssConfig(projectRoot) !== null || detectTailwindVite(projectRoot);
  const projectRequire = createProjectRequire(projectRoot);
  const esbuild = loadDependency('esbuild', projectRequire, projectRoot);
  const React = loadDependency('react', projectRequire, projectRoot);
  const ReactDOMServer = loadDependency('react-dom/server', projectRequire, projectRoot);
  const tempDir = createProjectTempDir(projectRoot);
  const outfile = path.join(tempDir, 'bundle.mjs');

  try {
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
      define: buildPublicEnvDefine(),
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
              const normalized = remainder === '' || remainder === '/'
                ? 'pyxle/client.js'
                : `pyxle${remainder}`;
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
              const descriptor = styleRegistry.describe(args.path, contents);
              const moduleCode = `const entry = ${JSON.stringify(descriptor)};
if (typeof globalThis.__PYXLE_REGISTER_SSR_STYLE__ === 'function') {
  globalThis.__PYXLE_REGISTER_SSR_STYLE__(entry);
}
export default entry.contents;
`;
              return {
                contents: moduleCode,
                loader: 'js',
                resolveDir: path.dirname(args.path),
              };
            });
          },
        },
      ],
      external: REACT_EXTERNALS,
    });

    const moduleUrl = pathToFileURL(outfile).href;
    const moduleExports = await import(moduleUrl);
    const Component = moduleExports.default ?? moduleExports.Component;

    if (typeof Component !== 'function') {
      throw new Error('Component does not export a default function.');
    }

    const headRegistry = createHeadRegistry();
    globalThis.__PYXLE_HEAD_REGISTRY__ = headRegistry;

    // Expose the request pathname / CSRF token to SSR code (e.g.
    // usePathname, <Form>'s hidden field). The subprocess renderer
    // receives both via env vars because the argv signature is stable
    // and argv already carries large JSON props.
    const requestPathname = process.env.PYXLE_REQUEST_PATHNAME;
    const csrfToken = process.env.PYXLE_CSRF_TOKEN;
    const previousPathname = globalThis.__PYXLE_CURRENT_PATHNAME__;
    const previousCsrf = globalThis.__PYXLE_CSRF_TOKEN__;
    if (typeof requestPathname === 'string' && requestPathname.length > 0) {
      globalThis.__PYXLE_CURRENT_PATHNAME__ = requestPathname;
    } else {
      delete globalThis.__PYXLE_CURRENT_PATHNAME__;
    }
    if (typeof csrfToken === 'string' && csrfToken.length > 0) {
      globalThis.__PYXLE_CSRF_TOKEN__ = csrfToken;
    } else {
      delete globalThis.__PYXLE_CSRF_TOKEN__;
    }

    try {
      const element = React.createElement(Component, props);
      const html = ReactDOMServer.renderToString(element);
      const styles = styleRegistry.list();
      const headElements = headRegistry.list();

      process.stdout.write(JSON.stringify({ ok: true, html, styles, headElements }));
    } finally {
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
    }
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
}

render().catch((error) => {
  process.stderr.write(
    JSON.stringify({
      ok: false,
      message: error.message,
      stack: error.stack,
    })
  );
  process.exit(1);
});

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
    if (path.basename(current) === 'client' && path.basename(path.dirname(current)) === '.pyxle-build') {
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
    `Unable to resolve '${specifier}'${location}. Install it in your project with 'npm install ${specifier}'.`,
    { cause: lastError }
  );
}

function redirectConsoleToStderr() {
  const redirected = new Console({ stdout: process.stderr, stderr: process.stderr });
  const methods = ['log', 'info', 'warn', 'error', 'debug', 'dir', 'trace'];

  for (const method of methods) {
    if (typeof redirected[method] === 'function') {
      console[method] = (...args) => redirected[method](...args);
    }
  }
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
      return {
        identifier: makeStyleIdentifier(source),
        source,
        contents,
      };
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
