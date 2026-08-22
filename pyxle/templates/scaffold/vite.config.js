// Root Vite config for this Pyxle project.
//
// Pyxle generates the real Vite configuration into
// `.pyxle-build/client/vite.config.js` every time you run `pyxle dev` or
// `pyxle build`, and drives Vite with that file directly. This root config
// simply re-exports it so the wider Vite ecosystem — `shadcn/ui` framework
// detection, editor integrations, and other tools that expect a config at the
// project root — can find one. You normally never edit this file; run
// `pyxle dev` / `pyxle build` rather than `vite` directly.
//
// It re-exports *lazily*, and that is deliberate: `.pyxle-build/` does not
// exist until the first `pyxle dev` or `pyxle build`, so a static
// `export { default } from ...` made this file throw ERR_MODULE_NOT_FOUND on a
// freshly scaffolded project — meaning the very tools it exists to satisfy
// found a config that crashes. Vite accepts a function returning a config, so
// resolution is deferred to the moment something actually asks.
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export default async () => {
    const generated = join(
        dirname(fileURLToPath(import.meta.url)),
        '.pyxle-build',
        'client',
        'vite.config.js',
    );
    // Existence is checked rather than catching ERR_MODULE_NOT_FOUND, because
    // that error cannot be told apart from the one it must not hide: when the
    // generated config imports a plugin that is not installed, Node names the
    // generated file in the message too — as the *importer*. Catching on the
    // message swallowed a genuine "plugin missing" failure into an empty
    // config. Not built yet is a file that is not there; anything else is a
    // real error and travels.
    if (!existsSync(generated)) return {};
    return (await import(pathToFileURL(generated).href)).default;
};
