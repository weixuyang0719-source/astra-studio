# Bundled UI fonts

All fonts are from the official Google Fonts repository, retrieved 2026-11-14, under SIL Open Font License 1.1. Matching licenses are included alongside the files. No system fonts are installed; no remote font request occurs at runtime.

- Noto Sans SC: https://github.com/google/fonts/tree/main/ofl/notosanssc (complete Chinese UI and native Qt fallback).
- Zen Maru Gothic Medium: https://github.com/google/fonts/tree/main/ofl/zenmarugothic (Latin brand accents only; not used for Chinese body text).
- Oxanium: https://github.com/google/fonts/tree/main/ofl/oxanium (transport numeric display only).

WOFF files are lossless format conversions via fontTools; outlines and naming are unchanged.

- Resource Han Rounded CN Medium 0.990: https://github.com/CyanoHao/Resource-Han-Rounded/releases/tag/v0.990. Official RHR-CN-0.990.7z, Medium TTF converted losslessly to WOFF without changing naming or outlines; SIL OFL 1.1 (resource-han-rounded-OFL.txt). Used only for titles and selected navigation/action labels; Chinese body text remains Noto Sans SC. Not installed system-wide.
