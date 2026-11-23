# Bus monitor

The monitor introduced in 1.18.1 captures interface reports continuously from
startup and shows an eight-column table with expandable record details. Raw reports,
decoded transaction summaries, and application diagnostics remain distinct.
The legacy `/api/monitor` endpoint retains its decoded-event contract for
existing consumers and switch learning.

## Capture lifetime and retention

Since 1.19.1, the Tridonic integration bounds USB response waits to two seconds.
A silent response triggers a reconnect and clears outstanding sequences; a
cancelled send with an outstanding request also reconnects. These are transport
failures, distinct from a valid DALI "no response" report. Normal sends and raw
report capture still use the pinned library's processing. Query notifications
remain visible here but do not trigger lamp refreshes.

The bridge attaches one monitor to its driver before connecting. Configuration
helpers reuse that monitor instead of creating separate callback subscriptions.
Capture continues while configuration operations pause bridge processing and
across USB disconnects and reconnects. Reconnecting does not erase history.

The shared buffer retains at most 10,000 records in memory, including raw reports,
decoded transaction summaries, and application diagnostics. Oldest records are
evicted first. Capture sequence numbers, capture identity, retention boundaries,
and eviction/error counters let consumers detect missing history. Nothing is
recorded continuously to disk or a database. Restarting the application loses
the buffer and changes its capture identity.

The legacy monitor keeps its original event fields and cursor semantics for
existing UI and switch-learning consumers. Its decoded events are derived from
the retained transaction records rather than stored in a second capture buffer.

## Driver capabilities

- **Tridonic:** a project-owned subclass captures each incoming USB report
  before calling the original python-dali 0.11 handler. It does not replace the
  library's transmit loop or transaction assembly. Reports are kept independently,
  including repeats, unsolicited backward frames, no-response notifications,
  initialization replies, malformed packets, and unknown report types.
- **hasseb:** decoded callback capture only. This interface cannot observe
  frames sent by other bus masters.
- **Other drivers:** decoded callback capture where the driver provides it.
  Capability metadata reports whether callback and connection monitoring are
  available; raw Tridonic coverage is not implied for other drivers. In
  decoded-only mode, use Transactions or All records to inspect traffic.

## Record information and interpretation

Versioned records preserve host receipt time in UTC with milliseconds, monotonic
elapsed time, classification, and available raw/decoded data. Unavailable values
are null. Tridonic records retain the complete report bytes, original numeric
mode/type codes, adapter sequence, and original interval value. The interval's
unit is not assumed. Transport/status data is not represented as a DALI frame.

Forward frames use python-dali decoding, the shared device/instance mapper, and
observed device-type context. Destinations have explicit gear/device namespaces
and short/group/broadcast kinds. Input events preserve protocol-supplied source,
instance/type/groups, and ten-bit event information, including unknown or
ambiguous events. Address and instance zero remain valid values. Backward zero
is a received frame; silence is a no-response notification. A framing-error
status does not establish valid DALI frame bytes.

Raw reports and decoded transaction summaries are distinct records. A decoded
callback may combine repeated configuration frames or associate a reply with a
command; that summary does not replace the original reports. Capture failures
and parsing failures are counted without preventing the original driver handler
from running. Transactions retain decoded reply text and available raw reply
bytes. Library error placeholders are labeled separately from captured bytes.
The configuration flag means **configuration repeat validation failed**; it
does not establish an exact failure cause. Named configuration operations record
start, completion, or failure and provide context on intervening capture records.

Host receipt time is not a precise wire timestamp. Capture includes only reports
delivered by the interface, with no guarantee of detecting losses inside the
adapter or USB transport. Adapter-reported origin is a hint: python-dali documents
a Tridonic firmware issue that can label an externally observed frame matching
the last transmitted frame as locally transmitted. Gear command destinations do
not identify the physical switch that sent them. Configuration-busy state does
not establish sender ownership.

## Table and controls

Columns appear in this order: **Line #**, **Type**, **Hex Data**, **Address**,
**Command**, **Time**, **Date**, **Comment**. Time/date use the browser's local
timezone with milliseconds. Expanded details show every stored field, including
UTC receipt time, elapsed time, full USB report, adapter metadata, raw/decoded
replies, and configuration context. Missing values display an em dash.

- **Reports** (default) includes every adapter report, including unknown/status
  packets. **Transactions** shows decoded callbacks; **All records** also shows
  application diagnostics.
- **find Hex**, **find Addr**, and **find Cmd** combine with AND and highlight
  matches without removing surrounding traffic. Hex ignores spaces, colons,
  hyphens, and an optional `0x` prefix. Address and command searches use
  case-insensitive substrings of their displayed columns. Invalid hex produces
  validation feedback. Enter finds next; Shift+Enter finds previous; navigation
  wraps and opens the matching page. Finding suspends automatic following.
- Optional class, query, address, and error filters start disabled. **Hide
  queries** does not claim that queries originate from the bridge.
- **Pause display** freezes the displayed snapshot while capture and polling
  continue. **Resume** catches up. **Follow latest** returns to the newest page
  and resumes following. **Clear view** affects this browser only.
- Client history retains at most 10,000 records and renders pages of 100.
  **Copy visible rows** copies the current table page. Retention/capture/error
  status and history gaps are shown explicitly.
- **Export JSONL** and **Export text** download all records retained by the
  server, regardless of the current view, pause state, clear action, or filters.

## HTTP and export formats

All UI requests use relative URLs for Home Assistant Ingress.

`GET api/monitor/records` accepts:

| Parameter | Meaning |
|---|---|
| `capture_id` | Optional UUID from the previous response; a mismatch resets history. |
| `since` | Nonnegative capture sequence, exclusive; default `0`. |
| `limit` | Page size from `1` to `500`; default `500`. |

Malformed or duplicated cursor/limit parameters return HTTP 400. Responses
contain `records`, `next`, `oldest`, `newest`, `has_more`, `capture_id`,
`capture_reset`, `history_gap`, `capabilities`, `counters`, and connection state.
Reconnects retain capture identity; application restarts change it. Clients
must drain pages sequentially and surface reset/gap indications. Legacy event
sequence numbers belong to the legacy API, not the capture sequence.

`GET api/monitor/export?format=jsonl|text` serializes one consistent snapshot of
all retained records. JSONL starts with a metadata object whose `record_type`
is `metadata`, followed by one complete record object per line. Text includes
the same metadata, readable summaries, and full JSON fields for each record.
Metadata includes schema/capture identity, retention boundaries, capability
notices, connection state, and eviction/capture/decode counters. Neither format
can recover evicted records or reports the interface never delivered.

Record groups include `adapter`, `frame`, `decoded`, `reply`, `diagnostics`,
`configuration`, and `application`. The optional `legacy` group preserves old
callback fields. The schema version is currently `1`; unavailable fields are
null. Hex values are uppercase and padded to known frame widths. Unsupported
widths remain raw adapter data without fabricated DALI frame values.

## Local verification

The hardware-free tests replay synthetic reports through the project subclass,
check original handler delivery and failure isolation, and exercise shared
registration, buffer overflow, configuration-busy capture, and connection
transitions. Physical USB, MQTT, and Home Assistant acceptance remain separate.

Run `node --test tests/test_monitor_ui.js` from the add-on directory for browser
state regressions using Node's built-in test runner. These complement the
recorded browser checks; they do not replace rendering or hardware acceptance.
