{#
  The window an incremental model recomputes on each run.

  The big tables (one row per customer and quarter hour, tens of millions of rows a year) are not
  rebuilt from scratch every morning: a run reworks only the last N whole local days, counted back
  from the newest day already in the table, and leaves everything older as it was. "Can still
  change" differs per model, so each one passes its own N, with the reason next to it. On the
  first build (or with --full-refresh) the predicate is simply true and the whole history is built.

  Whole local days, cut at Vienna midnight: an auction period never crosses midnight, so no
  hourly average is ever computed from part of its hour, and "the last 16 days" means 16 days.

  What a window cannot do, by construction:
  - a row for a day older than N days behind the table's newest day is never built or rebuilt.
    After a backfill of an old period, an outage longer than N days, or any change to a model's
    SQL or a dbt var, run `dbt build --full-refresh` for the affected models;
  - a row the query stops producing (inside the window) lingers until the next --full-refresh,
    because delete+insert only replaces the keys the new query emits.
  The singular test `allocation_is_current` compares the always-current staging view with the
  allocation mart, so the first of these fails the build instead of staying silent.
#}
{% macro recent_only(column, days) -%}
{%- if is_incremental() -%}
{{ column }} >= ((select coalesce(date_trunc('day', max(interval_start) at time zone 'Europe/Vienna'), timestamp '-infinity') from {{ this }}) - interval '{{ days }} days') at time zone 'Europe/Vienna'
{%- else -%}
true
{%- endif -%}
{%- endmacro %}
