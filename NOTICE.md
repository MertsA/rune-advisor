# Licences in this repository

Rune Advisor is a fan-made tool. It is not affiliated with or endorsed by Jagex Ltd. RuneScape is a trademark
of Jagex Ltd.

Created using intellectual property belonging to Jagex Limited under the terms of Jagex's Fan Content Policy.
This content is not endorsed by or affiliated with Jagex.

| What | Where | Licence |
|---|---|---|
| The planner, the export API, scripts, tests, the site's code | everything not listed below | GPL-3.0-or-later, see `LICENSE` |
| Game data: tables built from the RuneScape Wiki, hand-written tables, and quest and achievement definitions read from the game client | `src/rune_advisor/data/`, a separate repository included as a git submodule, and data packages built from it | not GPL: see `NOTICE.md` there (CC BY-NC-SA 3.0; the game definitions are Jagex's) |
| The in-game plugins | `plugins/` | GPL-3.0-or-later here; Jagex's terms on the Plugin Hub, see `plugins/LICENSE.md` |

Copyright (C) 2026 the Rune Advisor authors (see the git history).

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
option) any later version. It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See `LICENSE`.

## Game data

The tables the planner reads are a separate work from the program, kept in their own repository and included
here as a submodule at `src/rune_advisor/data/`. The GPL does not apply to them and their non-commercial
condition does not apply to the code. `NOTICE.md` in that folder says what applies to which file.

The planner's calculations implement game mechanics that the wiki's editors documented, and are checked against
the wiki's calculators. That code is written for this project and is GPL like the rest; `README.md`
("Acknowledgements") says what it owes them.

## Contributing

Contributions are accepted under GPL-3.0-or-later. Contributions to `plugins/` need one more grant, because
the plugins are also submitted to Jagex under Jagex's terms: see `plugins/LICENSE.md`.
