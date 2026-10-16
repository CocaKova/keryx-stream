# Third-party code

keryx-stream is MIT licensed (see [LICENSE](LICENSE)). It bundles or adapts:

| What | Where | Source | License |
|---|---|---|---|
| `gui_tour`'s schema, argument checks and messages, and its probe-then-full-deadline ladder, adapted for `keryx_tour` | `keryx_stream/tour.py` | [Hermes Agent, NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (`tools/tour_tool.py` and `tui_gateway/server.py` `_tour_request`, at `6fec745d45`) | MIT, [LICENSES/hermes-agent-MIT.txt](LICENSES/hermes-agent-MIT.txt) |
| QR Code generator library (Python), v1.8.0, unmodified | `keryx_stream/_qrcodegen.py` | [Project Nayuki, nayuki/QR-Code-generator](https://github.com/nayuki/QR-Code-generator) (`python/qrcodegen.py`, sha256 `b089855c…dcc84`) | MIT, [LICENSES/qrcodegen-MIT.txt](LICENSES/qrcodegen-MIT.txt) |

It draws the QR code `hermes keryx pair` prints, so pairing needs nothing installed beyond Hermes.
