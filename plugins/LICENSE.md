# Licence of the plugins

**In this repository** the source under `plugins/` is part of Rune Advisor and is licensed
GPL-3.0-or-later like everything else (`../LICENSE`).

**On the RuneScape Plugin Hub** it is not. Plugins submitted to Jagex are distributed through mod.io under
Jagex's End User Licence Agreement (Schedule 4) and mod.io's terms, and that schedule does not allow a plugin
there to carry any other terms. So the copy of a plugin you install from the Plugin Hub comes to you under
Jagex's and mod.io's terms, and the same source taken from this repository comes to you under the GPL. Both
are granted by the copyright holders; one does not limit the other.

The plugins were created using intellectual property belonging to Jagex Limited under the terms of Jagex's
Fan Content Policy. They are not endorsed by or affiliated with Jagex.

## Contributing to `plugins/`

A plugin can only be submitted if everyone who holds copyright in it allows it to be licensed on Jagex's
terms, and those terms can change after a contribution is merged. So a contribution to any file under
`plugins/` is accepted only with this grant, on top of the GPL:

> I license my contribution under GPL-3.0-or-later. I also grant the maintainers of this repository a
> perpetual, worldwide, non-exclusive, royalty-free, irrevocable licence to reproduce, modify and distribute
> my contribution as part of these plugins, and to license it to Jagex, mod.io and the plugins' users under
> whatever terms Jagex and mod.io require for RuneScape plugins at the time of a submission, including terms
> that change after my contribution. I keep my copyright. I have the right to make this grant.

You make the grant by adding a `Signed-off-by:` line to each commit that touches `plugins/`
(`git commit -s`). In this repository that line means the
[Developer Certificate of Origin 1.1](https://developercertificate.org/) and, for `plugins/`, the grant above.
Commits to `plugins/` without it are not merged.

Jagex's rules for plugin development also apply to contributions: do not paste or upload other developers'
plugin code into an AI model, and keep the code readable and commented for moderation.
