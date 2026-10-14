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
