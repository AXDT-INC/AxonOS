-- Upgrade an attested v5 database atomically.
BEGIN;
\ir 008_x_capi_v5_postflight.sql
ALTER TABLE x_capi_dedup DROP CONSTRAINT x_capi_dedup_milestone_check,
  ADD CHECK (milestone IN ('wallet_verified','deposit_completed','session_started',
                           'landing_page_viewed','app_launch_clicked','app_page_viewed'));
ALTER TABLE x_capi_outbox DROP CONSTRAINT x_capi_outbox_milestone_check,
  ADD CHECK (milestone IN ('wallet_verified','deposit_completed','session_started',
                           'landing_page_viewed','app_launch_clicked','app_page_viewed'));
UPDATE x_capi_schema_meta SET schema_version=6 WHERE singleton;
\ir 009_x_capi_v6_postflight.sql
COMMIT;
