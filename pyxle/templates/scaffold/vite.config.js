// Root Vite config for this Pyxle project.
//
// Pyxle generates the real Vite configuration into
// `.pyxle-build/client/vite.config.js` every time you run `pyxle dev` or
// `pyxle build`, and drives Vite with that file directly. This root config
// simply re-exports it so the wider Vite ecosystem — `shadcn/ui` framework
// detection, editor integrations, and other tools that expect a config at the
// project root — can find one. You normally never edit this file; run
// `pyxle dev` / `pyxle build` rather than `vite` directly.
export { default } from './.pyxle-build/client/vite.config.js';
