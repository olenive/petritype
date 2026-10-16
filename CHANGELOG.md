# Changelog

## Unreleased

- Nets as tokens: a `nested=True` place holds tokens that carry Petri nets of their own, which the outer net runs, logs and reaches inside, and a net saves and restores as JSON through a registered template (PROTOCOL.md §12).
- Token keys, queries and filters: places derive keys from their tokens, `Filter` and `Query` select by them (`Query.distinct` lists a key's values), `TakeWhereEdge` and `MutateWhereEdge` act on matching tokens only, and the protocol runner answers `query`, `distinct` and `set_filter` (PROTOCOL.md §13).
- `DrainEdge` and `SurveyEdge`, input edges that accept an empty place: a drain takes every token and a survey copies every token, zero or more either way.
- Marimo notebooks for the transit journey, the wildlife filter, the shop day, the traffic light junction and the rooms state machine.
- The traffic light (`examples/edges_on_empty_places/traffic_light.py`), a drain beside a plain read, with its shared transcript `tests/protocol/08_traffic_light.json`.
- Net-declared controls and place styles, `@petri_net(controls=..., place_styles=...)`, served as `topology.controls` and `places[].style`; the protocol's `press` runs a control, and the `Fire` command fires a named transition if it is fireable.
- A subclass of a JSON scalar, such as `class Colour(str)`, is serializable with its scalar's JSON form.

## 0.2.0, 2026-10-04

The [`v0.2.0` tag](https://github.com/olenive/petritype/tree/v0.2.0).
