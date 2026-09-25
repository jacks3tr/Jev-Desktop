# Jev Desktop feature map

| Feature | User paths | Observable proof |
| --- | --- | --- |
| [Discovery](discovery.md) | CLI inspect; MCP desktop_inspect | Linked app/window references, scoped controls, empty unmatched query |
| [Direct input](direct-input.md) | CLI act; MCP desktop_act | Fresh observation reflects the requested action |
| [Tasks](tasks.md) | CLI run --task / --spec; MCP desktop_run | Goal observed; workflow assertion and saved artifact agree |
| [Recovery](recovery.md) | CLI run/status/stop/evidence; MCP run/stop | Current token authorizes resume, stopped run cannot continue input |

The generated smoke covers discovery and unmatched-query filtering only. Each feature's other entry points remain separate coverage obligations when relevant to a change. Real input tests require exclusive desktop access and a disposable Notepad the run launches; unit doubles cannot substitute for those tests.
