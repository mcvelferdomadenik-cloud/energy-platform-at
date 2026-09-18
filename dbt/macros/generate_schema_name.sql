{# A model's +schema is the schema, not a suffix: staging lands in staging, not staging_staging. #}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ (custom_schema_name or target.schema) | trim }}
{%- endmacro %}
