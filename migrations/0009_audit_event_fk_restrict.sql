-- 0009_audit_event_fk_restrict.sql — P3-01 follow-up: append-only audit
-- events can no longer be deleted through their plan.
--
-- 0004 declared request_events / decision_events / delivery_events.plan_id
-- as REFERENCES plans (id) ON DELETE CASCADE. The backend role holds DELETE
-- on plans (0005), so deleting a plan silently removed append-only event
-- rows that the role is not allowed to DELETE directly. This migration
-- replaces exactly those three foreign keys with ON DELETE RESTRICT: a plan
-- that has audit events can no longer be deleted (foreign-key violation).
--
-- plan_steps / approval_requests keep CASCADE: the backend may already
-- DELETE those rows directly, so no append-only guarantee depends on them.
-- The constraint names are looked up in the catalog (0004 relied on default
-- naming) and the block is idempotent. No objects are created, so no grants
-- change (README rule 3); proven live by scripts/verify_db_grants.py.

DO $$
DECLARE
    target TEXT;
    fk RECORD;
BEGIN
    FOREACH target IN ARRAY ARRAY['request_events', 'decision_events', 'delivery_events'] LOOP
        FOR fk IN
            SELECT c.conname
            FROM pg_catalog.pg_constraint c
            JOIN pg_catalog.pg_attribute a
              ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
            WHERE c.conrelid = format('public.%I', target)::regclass
              AND c.contype = 'f'
              AND c.confrelid = 'public.plans'::regclass
              AND a.attname = 'plan_id'
              AND c.confdeltype <> 'r'
        LOOP
            EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT %I', target, fk.conname);
        END LOOP;

        IF NOT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_constraint c
            JOIN pg_catalog.pg_attribute a
              ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
            WHERE c.conrelid = format('public.%I', target)::regclass
              AND c.contype = 'f'
              AND c.confrelid = 'public.plans'::regclass
              AND a.attname = 'plan_id'
              AND c.confdeltype = 'r'
        ) THEN
            EXECUTE format(
                'ALTER TABLE public.%I ADD CONSTRAINT %I FOREIGN KEY (plan_id) '
                'REFERENCES public.plans (id) ON DELETE RESTRICT',
                target,
                target || '_plan_id_fkey'
            );
        END IF;
    END LOOP;
END
$$;
