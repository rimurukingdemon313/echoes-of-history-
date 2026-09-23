# Branding

Optional. The thumbnail engine works without anything here: it draws the
title over a plate taken from the documentary's own visuals, using a serif
face from the container image, with a measured-contrast scrim so the text
stays legible over an arbitrary photograph.

If you want a channel mark on every thumbnail, drop `logo.png` here (PNG with
transparency, at least 400px on its long edge) and extend
`echoes/pipeline/thumbnail.py:_render_concept`.

Channel banner and avatar are set in YouTube Studio, not by this system.
