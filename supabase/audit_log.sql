-- The record of every calculation the pricing tool makes. Run once in the Supabase SQL editor.
--
-- Rows are written by the signed-in person with their own token, so the policy is the control:
-- a person may insert a row about themselves and nothing else. There is no update and no delete
-- policy, for anyone; the record is append-only the way `raw` is in the warehouse. Admins read it.
-- Roles live in each user's app_metadata as {"role": "sales" | "support" | "analyst" | "admin"},
-- set in Authentication -> Users; the tool refuses an account without one.

create table if not exists public.audit_log (
    id        bigint generated always as identity primary key,
    at        timestamptz not null default now(),
    user_id   uuid not null default auth.uid() references auth.users (id),
    email     text not null,
    role      text not null,
    event     text not null,
    customer  text,
    contract  text,
    detail    jsonb not null default '{}'::jsonb
);

alter table public.audit_log enable row level security;

create policy "a person records only their own actions"
    on public.audit_log for insert to authenticated
    with check ((select auth.uid()) = user_id);

create policy "admins read the record"
    on public.audit_log for select to authenticated
    using ((select auth.jwt() -> 'app_metadata' ->> 'role') = 'admin');

revoke all on public.audit_log from anon;
revoke update, delete, truncate on public.audit_log from authenticated;
grant insert, select on public.audit_log to authenticated;
