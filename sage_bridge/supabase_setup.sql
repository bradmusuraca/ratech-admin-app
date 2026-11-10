-- Run in Supabase: SQL Editor -> New query -> paste -> Run
-- Creates the order queue the app writes to and the bridge reads.

create table if not exists sage_order_queue (
    order_key      text primary key,
    status         text not null default 'pending'
                   check (status in ('pending','processing','done','error')),
    payload        jsonb not null,
    sage_order_no  text,
    error          text,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

-- service_role key bypasses RLS entirely; no policies needed since only
-- the app (server-side) and bridge hold that key.
alter table sage_order_queue enable row level security;

-- Distinguishes 'create' sales-order jobs from 'qty_update' edits the
-- packager's short-ship marks generate.
alter table sage_order_queue add column if not exists job_type text not null default 'create';

-- Packing list: one row per Sage transaction sent to the floor.
create table if not exists open_orders (
    order_key      text primary key,
    customer_id    text,
    customer_po    text,
    status         text not null default 'open'
                   check (status in ('open','short','complete')),
    lines          jsonb not null,
    sage_order_no  text,
    packed_by      text,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

alter table open_orders enable row level security;

-- Re-saved jobs get 'superseded' - allow it in the status check.
alter table sage_order_queue drop constraint if exists sage_order_queue_status_check;
alter table sage_order_queue add constraint sage_order_queue_status_check
    check (status in ('pending','processing','done','error','superseded'));

-- Source document (order PDF) kept per order so the packing page can show
-- the original paperwork next to the freight widget. Base64 text keeps it
-- in Postgres - no Storage bucket to configure.
create table if not exists order_docs (
    order_key      text primary key,
    file_name      text,
    pdf_b64        text,
    created_at     timestamptz not null default now()
);

alter table order_docs enable row level security;

-- Third-party FedEx billing per customer: when set, the Create Invoices
-- freight widget auto-fills the customer's FedEx account.
create table if not exists fedex_profiles (
    customer_id    text primary key,
    acct           text,
    zip            text,
    created_at     timestamptz not null default now()
);

alter table fedex_profiles enable row level security;

-- Ratech item descriptions synced from Sage by the bridge, so sales
-- orders/pick lists carry our wording instead of the customer's.
create table if not exists items (
    item_id        text primary key,
    description    text,
    updated_at     timestamptz not null default now()
);

alter table items enable row level security;

-- Remote pause switch for the bridge (app sidebar "Pause Sage bridge").
-- While paused, the bridge releases its Sage session so tasks that need
-- single-user mode (bulk price changes, backups) don't see "another user".
-- bridge_state/bridge_seen_at are reported by the bridge itself.
create table if not exists bridge_control (
    id             int primary key default 1 check (id = 1),
    paused         boolean not null default false,
    paused_by      text,
    paused_at      timestamptz,
    bridge_state   text,
    bridge_seen_at timestamptz
);

alter table bridge_control enable row level security;
insert into bridge_control (id) values (1) on conflict (id) do nothing;

-- exec_sql: lets agents run arbitrary SQL through PostgREST (DDL included)
-- instead of needing the SQL Editor or a direct Postgres connection (the
-- VPN on Brad's machine blocks non-443 ports anyway).
--   POST /rest/v1/rpc/exec_sql  {"query": "select now()"}
-- SELECT/INSERT...RETURNING return rows as jsonb; DDL/DML return {"ok":true}.
-- SECURITY DEFINER + execute granted only to service_role, so it inherits
-- the key's full access - never expose the service key client-side.
create or replace function exec_sql(query text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    rows jsonb;
begin
    -- try as a row-returning statement first; the __r alias must be unusual
    -- so a column literally named 't' can't shadow the whole-row reference
    execute 'select coalesce(jsonb_agg(__r), ''[]''::jsonb) from (' || query || ') __r'
        into rows;
    return rows;
exception when others then
    -- DDL / DML without RETURNING can't be wrapped in a subquery - run it
    -- directly. If the query itself was bad, this re-raises the real error.
    execute query;
    return jsonb_build_object('ok', true);
end;
$$;

revoke all on function exec_sql(text) from public, anon, authenticated;
grant execute on function exec_sql(text) to service_role;

-- Remote command channel: agents INSERT rows, sage_cmd.ps1 on the Sage PC
-- polls every ~15s, executes, and writes output back. Same trust domain as
-- the bridge (service-role only) - the key already posts real orders.
create table if not exists sage_commands (
    id          bigint generated always as identity primary key,
    command     text not null,
    status      text not null default 'pending'
                check (status in ('pending','running','done','error')),
    output      text,
    created_by  text,
    created_at  timestamptz not null default now(),
    started_at  timestamptz,
    finished_at timestamptz
);

alter table sage_commands enable row level security;
