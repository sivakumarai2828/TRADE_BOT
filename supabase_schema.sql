-- TRADE_BOT_V2 tables. Run once in Supabase SQL Editor.
-- v2_ prefix keeps them separate from any old V1 tables.
-- RLS is enabled with NO public policies: only the secret key (on the VM)
-- can read/write; the publishable key sees nothing.

create table if not exists v2_trades (
    id bigint generated always as identity primary key,
    ts_open double precision, ts_close double precision,
    market text, symbol text, side text,
    qty double precision, entry double precision, exit_price double precision,
    stop double precision, target double precision,
    pnl double precision, pnl_pct double precision,
    thesis text, exit_reason text,
    status text default 'open'
);

create table if not exists v2_decisions (
    id bigint generated always as identity primary key,
    ts double precision, market text, raw_json text, note text
);

create table if not exists v2_memos (
    id bigint generated always as identity primary key,
    ts double precision, market text, kind text, body text
);

create table if not exists v2_kv (k text primary key, v text);

alter table v2_trades enable row level security;
alter table v2_decisions enable row level security;
alter table v2_memos enable row level security;
alter table v2_kv enable row level security;
