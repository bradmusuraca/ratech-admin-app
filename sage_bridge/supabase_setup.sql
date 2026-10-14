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
