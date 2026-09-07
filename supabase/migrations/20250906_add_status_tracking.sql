-- Add status tracking columns for efficiency metrics
ALTER TABLE public.orders
  ADD COLUMN IF NOT EXISTS status_prep_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS status_packed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS status_out_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS status_done_at TIMESTAMPTZ;

-- Add indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_orders_status_prep_at ON public.orders(status_prep_at);
CREATE INDEX IF NOT EXISTS idx_orders_status_packed_at ON public.orders(status_packed_at);
CREATE INDEX IF NOT EXISTS idx_orders_status_out_at ON public.orders(status_out_at);
CREATE INDEX IF NOT EXISTS idx_orders_status_done_at ON public.orders(status_done_at);
