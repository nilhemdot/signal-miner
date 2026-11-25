# Project lessons (Clawness memory)
<!-- Clawness retrieves from this file each prompt: `## Always` entries are
     injected every turn (keep to 3), `## Lessons` entries only when they match
     the prompt. Tell Claude "remember this: ..." or append a bullet yourself
     (one line, <=120 chars, newest at the bottom). Only lessons that would cost
     real rework if forgotten belong here — not a session log. See ENF-MEM-001. -->

## Always

## Lessons
- sec.gov 403 means a missing User-Agent OR a file that does not exist; sec/index.py checks index.json before fetching.
- New signal rules never delete old rows in sm.signals; rebuild a dev DB (sm_dev) after changing loader._signal_for.
