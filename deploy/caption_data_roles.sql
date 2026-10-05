-- Apply with the matching Project1B backend role allowlist change.
-- Expands allowed grants; preserves all accounts and existing role assignments.
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE user_roles DROP CONSTRAINT ck_user_roles_role;
ALTER TABLE user_roles ADD CONSTRAINT ck_user_roles_role CHECK (
    role IN ('worker', 'admin', 'record_manager', 'cut_manager', 'caption_manager',
             'caption_data_admin', 'caption_data_reviewer')
);
COMMIT;
