# Changelog

## Unreleased

- The protocol runner speaks version 2 of the net protocol (PROTOCOL.md §10). An inner net's `path` is a list of steps from the loaded net down, `{"place", "token", "net"}` for a net carried by a token, in requests, results, a token's `nets`, the `nested` notification, the `step` result and the log, so a place or a net may be named with any character and still be addressed. A path of any other shape is `invalid_params`, and one that names no live net is `unknown_net`, naming the step that failed by its index from 0. A net's progress counts only the firings of its own transitions: the loaded net's `fired_counts` no longer holds a joined name for each firing inside its tokens. The shared transcripts are re-recorded for version 2, and the transcript recorder keeps the names and masks of a transcript it re-records.

## 0.3.1, 2026-10-17

The [`v0.3.1` tag](https://github.com/olenive/petritype/tree/v0.3.1).

- Sequential runs notify observers as a transition goes in flight, before its body runs, as concurrent runs already did, so a renderer on the event loop can show it working; an observer that raises at that notification fails the firing as a raising body does, with a `LaunchNotificationError` as the `TransitionFailedError`'s cause.

## 0.3.0, 2026-10-17

The [`v0.3.0` tag](https://github.com/olenive/petritype/tree/v0.3.0).

- Nets as tokens: a `nested=True` place holds tokens that carry Petri nets of their own, which the outer net runs, logs and reaches inside, and a net saves and restores as JSON through a registered template (PROTOCOL.md §12).
- Token keys, queries and filters: places derive keys from their tokens, `Filter` and `Query` select by them (`Query.distinct` lists a key's values), `TakeWhereEdge` and `MutateWhereEdge` act on matching tokens only, and the protocol runner answers `query`, `distinct` and `set_filter` (PROTOCOL.md §13).
- `DrainEdge` and `SurveyEdge`, input edges that accept an empty place: a drain takes every token and a survey copies every token, zero or more either way.
- Marimo notebooks for the transit journey, the wildlife filter, the shop day, the traffic light junction and the rooms state machine.
- The traffic light (`examples/edges_on_empty_places/traffic_light.py`), a drain beside a plain read, with its shared transcript `tests/protocol/08_traffic_light.json`.
- Net-declared controls and place styles, `@petri_net(controls=..., place_styles=...)`, served as `topology.controls` and `places[].style`; the protocol's `press` runs a control, and the `Fire` command fires a named transition if it is fireable.
- A subclass of a JSON scalar, such as `class Colour(str)`, is serializable with its scalar's JSON form.

## 0.2.0, 2026-10-04

The [`v0.2.0` tag](https://github.com/olenive/petritype/tree/v0.2.0).
