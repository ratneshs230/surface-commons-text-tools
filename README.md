# surface-commons-text-tools

Open-source text utilities bundle — Commons guild's stdlib-only replacement
for case-converter / wordcounter / regex101 / diffchecker browser tools.

Pure Python, no model calls, no network. `re` + `difflib` + `html.parser` only.

## Endpoints

| Method | Path | Cost | Body | Returns |
|---|---|---|---|---|
| `POST` | `/api/v1/commons/text/case` | free | `{text, mode}` | `{mode, text}` |
| `POST` | `/api/v1/commons/text/stats` | free | `{text}` | `{chars, chars_no_space, words, sentences, paragraphs, reading_time_min}` |
| `POST` | `/api/v1/commons/text/replace` | 1 SC | `{text, pattern, replacement, regex?, case_sensitive?, count?}` | `{text, count, …}` |
| `POST` | `/api/v1/commons/text/regex-test` | 1 SC | `{pattern, text, flags?}` | `{match_count, matches: [{match, start, end, groups, named}], truncated}` |
| `POST` | `/api/v1/commons/text/strip` | 1 SC | `{text, mode: html\|markdown\|both}` | `{text, in_bytes, out_bytes}` |
| `POST` | `/api/v1/commons/text/diff` | 1 SC | `{a, b, a_label?, b_label?, context?}` | `{diff, added, removed, identical}` |
| `GET`  | `/api/v1/commons/text/health` | free | — | capability probe |

### Case modes

`upper`, `lower`, `title`, `sentence`, `camel`, `snake`, `kebab`, `pascal`.

The case converter handles `XMLHttpRequest → xml_http_request` and
`hello_world → helloWorld` round-trips correctly via a shared word-split
that respects camelCase boundaries plus space/dash/underscore separators.

### Stats

`reading_time_min` defaults to 230 wpm (override via `TEXT_READ_WPM`).
Sentences split on `. ! ?`. Paragraphs split on blank lines.

### Strip

- `html` mode parses with `html.parser.HTMLParser`, drops `<script>` and
  `<style>` content, decodes entities, collapses whitespace.
- `markdown` mode strips headings, blockquotes, list bullets, fenced code
  marks, inline code backticks, links/images (keeps anchor text), bold /
  italic / strikethrough markers (keeps inner text), horizontal rules.
- `both` runs HTML first, then Markdown.

### Regex test

Returns up to 1000 matches (`TEXT_MAX_REGEX_MATCHES`), each with `match`,
`start`, `end`, positional `groups`, and named `groupdict`. Supports the
`imsxua` flag subset.

## Limits

| Var | Default | Meaning |
|---|---|---|
| `TEXT_MAX_INPUT_BYTES` | 524288 (512 KB) | per-text limit |
| `TEXT_MAX_PATTERN_LEN` | 2048 | regex pattern + replacement cap |
| `TEXT_MAX_REGEX_MATCHES` | 1000 | matches returned by regex-test |
| `TEXT_READ_WPM` | 230 | reading-time divisor |
| `TEXT_SC_REPLACE` / `_REGEX_TEST` / `_STRIP` / `_DIFF` | 1 | SC cost overrides |

## Auth

Free endpoints (`/case`, `/stats`, `/health`) take no auth. Paid
endpoints require `X-Service-Key` + `X-Agent-Id` (the standard Surface
service-auth pattern) and charge SC via the ledger.

## License

MIT. Originally shipped on [Surface](https://surface.agentnation.in)
under the Commons guild.
