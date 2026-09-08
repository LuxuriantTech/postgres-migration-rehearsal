# Browser dependency provenance, 2026-09-08

LOW02 remains OPEN, with stronger npm evidence. This is separate from the PostgreSQL image baseline and does not affect the hosted static demonstration, which loads no browser-testing dependency.

The locked tree has 113 package entries. Every upstream npm archive was downloaded from its exact registry.npmjs.org resolved URL and its lockfile SRI verified. 111 installed packages matched all regular archive members directly. The optional Darwin-only fsevents package is absent on Linux. robots-parser contains .gitignore upstream; npm installs it as .npmignore. An independent npm ci --ignore-scripts installation with a fresh cache produced an identical complete installed file/link inventory, including that renamed file. No installed hash alone is used to claim an upstream match.

Reproduction: retain package.json and package-lock.json, use Node 24.15.0 and npm 11.18.0, then run npm ci --ignore-scripts --no-audit --no-fund with an empty task-owned cache in a separate directory. Verify archive SRI from each lockfile resolved URL and compare the rebuilt node_modules tree, including generated links. Installation scripts were not executed.

The declared authoring Node version is 24.15.0. The fixed Playwright image actually provides Node 24.18.1 on Linux x64. This is a host/container environment difference, not a changed package-lock version. The image is mcr.microsoft.com/playwright@sha256:dcc5531e97840b9b5e794f2814476b21571c5124a3fca2267d73041f56e7580e; its upstream OCI manifest was fetched again. Playwright is 1.62.1 and axe integration 4.13.0.

Remaining gap: this work does not independently reconstruct the browser executables and all supporting OS libraries from their original upstream distributions. Trust in the fixed Microsoft image and the local Docker administrator remains. The historical browser producer also accepts test filters without recording a complete invocation. Its old receipt cannot prove an exhaustive matrix. Therefore LOW02 is not closed and no historical test matrix is rerun or relabelled PASS.
