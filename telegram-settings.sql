UPDATE settings SET value = jsonb_set(
  coalesce(value, '{}'::jsonb),
  '{bot_token}',
  'REDACTED'::jsonb,
  true
) WHERE key = 'app';

UPDATE settings SET value = jsonb_set(
  coalesce(value, '{}'::jsonb),
  '{chat_id}',
  '"@f2d_order"'::jsonb,
  true
) WHERE key = 'app';

UPDATE settings SET value = jsonb_set(
  coalesce(value, '{}'::jsonb),
  '{telegram_enabled}',
  'true'::jsonb,
  true
) WHERE key = 'app';
