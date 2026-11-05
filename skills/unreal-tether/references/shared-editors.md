# Shared editors: leases, second editors, sub-agents

The rules are in SKILL.md. This is the detail behind them.

## The user's editor

If the user has the project open in their own editor, `ue` works with that one (`ue status` says "opened by hand"):
do not start a second editor, and do not close or restart theirs (`ue build --restart` included) without asking;
the user may be working in it, so say before you save. With two editors open (`ue status` lists both), commands
go to the user's; `ue --pid <pid> ...` picks the other, and saving the same assets from both loses work.

## Other sessions and the lease

Other Claude sessions may share this editor (`ue who` lists who ran what in the last 15 min, and who started PIE).
Before anything that takes the editor away from them (a map rebuild, a long PIE run, `ue build --restart`,
`ue close`), run `ue lease take --for "map rebuild + PIE"` and `ue lease release` when done; `ue status` shows the
lease to everyone. While another session holds it, `build --restart`, `close`, `pie start`, a UBT build with no
editor open, and anything that writes to disk (`save`, `import`, `mv`, `rm`, `--save`) refuse with exit 5 and say
who holds it and for what (edits in memory go ahead, with a note; `lease take` exits 5 too, so chain with `&&`, not
`;`): wait, or `ue lease request --for "why"` (the holder sees it on its next
command and runs `ue lease grant` when done), rather than passing `--ignore-lease`; `ue wait lease` then
returns once it is yours or free and PIE has stopped. Asked for yours? Grant it when you reach a stopping point.
A lease lasts at least 5 minutes past the end of its holder's last `ue` command, and is held while one of its
commands runs (a long `ue py` stage), so it does not run out mid-task; `status`, `who` and `lease` show "busy:
<command> for N min" for a command in flight (a busy editor, not a hung one). But while someone waits, a lease lapses
after 3 minutes with no `ue` command of yours and no PIE session of yours running: do the thinking and editing
before you take it, run the editor work as one script, and release it when done (`take --hold` only for work in the
editor that a hand-over would lose, e.g. unsaved edits you are still making). A test script that runs `ue` with its
output thrown away never sees requests: call `ue lease yield --back` between suites (hands the lease to a waiting
request, then waits to get it back; `ue lease pending` exits 0 when someone waits). A script you run inside your
own lease can take and release its own: its lease goes on top of yours, and its release gives yours back. Work that
needs another map or a throwaway import while others use the editor: `ue open --second` starts your own headless
editor next to the shared one (its own port; your commands go there, others' do not; no lease needed; no
pre-build, as the binaries are in use), and `ue close` closes it as soon as you are done: while it runs, the shared
editor cannot save the packages it loaded (Windows keeps their files open), so it refuses to start while the shared
one holds unsaved work. A restart or
close also asks for the lease when another session used the editor in the last 5 minutes. A build error in a file
you did not touch says who last edited it ("edited by spire ... (not you)"): leave it to that session.
`ue who --as NAME` gives your session a name the others see. Committing from a shared checkout: `ue git stage
--mine` stages only the files your session wrote last (Edit/Write, and files your Python scripts or `ue py`
builders wrote, read from the session transcripts), and names the ones another session also wrote since their
last commit (stage your hunks with `git add -p`), the ones it wrote, and the ones it cannot place.

## Which editor your commands reach

With several editors open, each session keeps talking to the one it opened or used last (`--pid` changes
that), so an editor the user opens mid-session does not take your commands. A new session starts on the editor
the lease holder and the other sessions use; `ue status` alone does not pin one. Sub-agents (the Agent tool)
count apart here: each keeps its own editor, its own `ue who --as` name, and a `--second` editor one agent opens
is the default for that agent only (the lease still covers the whole session). Tether tells them apart by their
transcripts; for an `ue` call made from inside a script it cannot, so pass `--pid` there.
