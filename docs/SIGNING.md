# Code signing (SignPath Foundation)

Releases are signed for free by [SignPath Foundation](https://signpath.org), which
issues certificates to open-source projects. The repo side is done: the
workflow (`.github/workflows/release.yml`) and the two artifact configurations
(`.signpath/artifact-configurations/`). What's left needs the maintainer's
accounts:

1. **Apply** at [signpath.org/apply](https://signpath.org/apply) with:
   - repository: `https://github.com/GeorgeAzma/pc-remote`, licence MIT;
   - download page: the GitHub releases page;
   - what it is: a remote control for your own Windows PC from your phone's
     browser. It's installed openly (an installer, a Start menu entry and an
     uninstaller), sign-in is on by default, and it only listens on home
     networks and Tailscale;
   - the code signing policy: the [README section](../README.md#code-signing-policy).

   Turn on multi-factor authentication on GitHub and SignPath first: SignPath
   requires it for everyone with commit or approval rights.

2. **Once approved**, SignPath sets up an organization with a project. In it:
   - The project's slug must be `pc-remote` (the workflow uses it).
   - Add artifact configurations with slugs `app` and `installer`, pasting
     `app.xml` and `installer.xml` from `.signpath/artifact-configurations/`.
   - Connect GitHub.com as the trusted build system, so that only this
     repo's workflow can submit signing requests.
   - The signing policies are `test-signing` (used by runs started by hand)
     and `release-signing` (used for tags; each request waits for an
     approver in SignPath).
   - Create a CI user, give it submitter rights on both policies, and copy
     its API token.

3. **In GitHub** (Settings → Secrets and variables → Actions):
   - secret `SIGNPATH_API_TOKEN`: the CI user's token;
   - variable `SIGNPATH_ORGANIZATION_ID`: from SignPath's organization settings.

4. **Try it**: Actions → Release → Run workflow. That signs with the test
   certificate and checks the configurations: file names, product name
   `PC Remote` and the version. Then push a tag; approve the two signing
   requests in SignPath (the app, then the installer) and the release gets the
   signed installer.

What's signed: only `PC Remote.exe` and the installer, which are built from
this repo. The Python runtime, ffmpeg and library files inside are third-party
and stay as they are, as SignPath Foundation requires. The uninstaller
(`unins000.exe`) isn't signed, because Inno Setup creates it while compiling and
SignPath signs afterwards.
