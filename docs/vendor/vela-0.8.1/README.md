# Vela 0.8.1

Unmodified production browser bundle from the official npm package
`@luxalgo/vela@0.8.1` (https://www.npmjs.com/package/@luxalgo/vela/v/0.8.1).
Source: https://github.com/LuxAlgo/Vela

The adjacent `LICENSE` and `NOTICE` are copied verbatim from the package.
Keep them and the visible Vela attribution when redistributing the chart.
The built-in attribution remains enabled as well.

SHA-256 of `vela.global.min.js`:
`40f39542d99863bda652c225c84e2d28c28d25c1604447111e77b76bfc135e45`

To reproduce: `npm pack @luxalgo/vela@0.8.1 --ignore-scripts`, then extract
`package/dist/vela.global.min.js`, `package/LICENSE` and `package/NOTICE`.
There is no application build step. Do not replace the pinned file with a
floating CDN reference. Verify a proposed version upgrade and rerun chart tests.

The sample page uses only `Vela.Vela` with locally generated offline bars and
`live: false`. The distribution also contains provider classes, but none are
instantiated or registered. The page's Content Security Policy forbids network
connections (`connect-src 'none'`). No scripting engine is bundled by the app.
