# Vendored: fontawesome5

`fontawesome5.tar.xz` is the LaTeX `fontawesome5` package (the contact icons
in the CV header), vendored here so the image build never has to reach CTAN.

| | |
|---|---|
| Source | `https://mirror.ctan.org/systems/texlive/tlnet/archive/fontawesome5.tar.xz` |
| Package | TeX Live archive format — a `texmf` tree, unpacked straight into `TEXMFLOCAL` by [`../install-fontawesome5.sh`](../install-fontawesome5.sh) |
| TeX Live revision | 77682 |
| Fetched | 2026-09-15 |
| SHA-256 | `dc0df9192cdb088eb7bca42753128b0c464e61836f02b1fe5dc994c528a51ea4` |
| License | LaTeX code: LPPL 1.3c+ (© Marcel Krueger). Fonts: SIL OFL 1.1 (Font Awesome 5 Free, by Fonticons, Inc.). Both permit redistribution. |

Why vendored rather than fetched at build time: Debian ships `fontawesome5`
only inside `texlive-fonts-extra` (1.7 GB for about 1 MB of font), and a
build step that reaches out to CTAN is one more way the build can fail (a
mirror redirect, a moved archive — see the git history of
`install-fontawesome5.sh` for the 404 that motivated this).

## Refreshing

```bash
curl -o docker/vendor/fontawesome5.tar.xz \
    https://mirror.ctan.org/systems/texlive/tlnet/archive/fontawesome5.tar.xz
sha256sum docker/vendor/fontawesome5.tar.xz   # update the table above
```

Then `docker build` and confirm `kpsewhich fontawesome5.sty` resolves inside
the image (`install-fontawesome5.sh` fails the build itself if it doesn't).
