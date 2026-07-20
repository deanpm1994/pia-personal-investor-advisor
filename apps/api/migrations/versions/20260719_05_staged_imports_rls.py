"""Add private, append-only staged-import persistence.

Revision ID: 20260719_05
Revises: 20260716_04
Create Date: 2026-07-19 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260719_05"
down_revision: str | Sequence[str] | None = "20260716_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create private staged-import records without managing Supabase Storage."""
    op.execute(
        """
        CREATE TABLE public.staged_imports (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
            source_provider text NOT NULL CHECK (btrim(source_provider) <> ''),
            source_format text NOT NULL CHECK (btrim(source_format) <> ''),
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            UNIQUE (id, user_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.staged_import_files (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
            staged_import_id uuid NOT NULL,
            bucket_id text NOT NULL CHECK (bucket_id = 'raw-imports'),
            object_path text NOT NULL CHECK (
                btrim(object_path) <> ''
                AND split_part(object_path, '/', 1) = user_id::text
                AND position('/' IN object_path) > 1
                AND btrim(split_part(object_path, '/', 2)) <> ''
            ),
            filename text NOT NULL CHECK (btrim(filename) <> ''),
            content_type text NOT NULL CHECK (btrim(content_type) <> ''),
            byte_size bigint NOT NULL CHECK (byte_size > 0),
            sha256 text NOT NULL CHECK (sha256 ~ '^[0-9A-Fa-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            UNIQUE (staged_import_id),
            UNIQUE (id, staged_import_id, user_id),
            FOREIGN KEY (staged_import_id, user_id)
                REFERENCES public.staged_imports (id, user_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.staged_import_rows (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
            staged_import_id uuid NOT NULL,
            source_row_number integer NOT NULL CHECK (source_row_number > 0),
            source_row jsonb NOT NULL CHECK (
                jsonb_typeof(source_row) = 'object' AND source_row <> '{}'::jsonb
            ),
            parsed_output jsonb CHECK (
                parsed_output IS NULL
                OR (
                    jsonb_typeof(parsed_output) = 'object'
                    AND parsed_output <> '{}'::jsonb
                )
            ),
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            UNIQUE (staged_import_id, source_row_number),
            UNIQUE (id, staged_import_id, user_id),
            FOREIGN KEY (staged_import_id, user_id)
                REFERENCES public.staged_imports (id, user_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.staged_import_validation_results (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
            staged_import_id uuid NOT NULL,
            staged_import_row_id uuid,
            code text NOT NULL CHECK (btrim(code) <> ''),
            severity text NOT NULL CHECK (severity IN ('error', 'warning', 'info')),
            message text NOT NULL CHECK (btrim(message) <> ''),
            details jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            FOREIGN KEY (staged_import_id, user_id)
                REFERENCES public.staged_imports (id, user_id) ON DELETE CASCADE,
            FOREIGN KEY (staged_import_row_id, staged_import_id, user_id)
                REFERENCES public.staged_import_rows (id, staged_import_id, user_id)
                ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.staged_import_state_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
            staged_import_id uuid NOT NULL,
            position integer NOT NULL CHECK (position > 0),
            state text NOT NULL CHECK (state IN (
                'staged', 'parsed', 'validated', 'review_ready', 'confirmed', 'blocked'
            )),
            details jsonb,
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            UNIQUE (staged_import_id, position),
            FOREIGN KEY (staged_import_id, user_id)
                REFERENCES public.staged_imports (id, user_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.enforce_staged_import_state_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            previous_position integer;
            previous_state text;
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(NEW.staged_import_id::text, 0)
            );

            SELECT position, state
            INTO previous_position, previous_state
            FROM public.staged_import_state_events
            WHERE staged_import_id = NEW.staged_import_id
            ORDER BY position DESC
            LIMIT 1;

            IF NOT FOUND THEN
                IF NEW.position <> 1 OR NEW.state <> 'staged' THEN
                    RAISE EXCEPTION
                        'staged import % must begin with staged at position 1',
                        NEW.staged_import_id;
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM public.staged_import_files
                    WHERE staged_import_id = NEW.staged_import_id
                ) THEN
                    RAISE EXCEPTION 'staged import % requires one raw-imports file',
                        NEW.staged_import_id;
                END IF;
            ELSIF NEW.position <> previous_position + 1 THEN
                RAISE EXCEPTION 'staged import % event positions must be consecutive',
                    NEW.staged_import_id;
            ELSIF NOT (
                (previous_state = 'staged' AND NEW.state = 'parsed')
                OR (previous_state = 'parsed' AND NEW.state = 'validated')
                OR (previous_state = 'validated'
                    AND NEW.state IN ('review_ready', 'blocked'))
                OR (previous_state = 'review_ready' AND NEW.state = 'confirmed')
            ) THEN
                RAISE EXCEPTION 'invalid staged import transition from % to %',
                    previous_state, NEW.state;
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER staged_import_state_events_transition_check
        BEFORE INSERT ON public.staged_import_state_events
        FOR EACH ROW EXECUTE FUNCTION public.enforce_staged_import_state_transition()
        """
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.enforce_staged_import_state_transition() "
        "FROM PUBLIC"
    )

    for table in (
        "staged_imports",
        "staged_import_files",
        "staged_import_rows",
        "staged_import_validation_results",
        "staged_import_state_events",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT, INSERT ON public.{table} TO authenticated")
        op.execute(
            f"""
            CREATE POLICY {table}_select_own ON public.{table}
            FOR SELECT TO authenticated
            USING ((SELECT auth.uid()) = user_id)
            """
        )
        op.execute(
            f"""
            CREATE POLICY {table}_insert_own ON public.{table}
            FOR INSERT TO authenticated
            WITH CHECK ((SELECT auth.uid()) = user_id)
            """
        )


def downgrade() -> None:
    """Remove the P4.2 application-owned staged-import boundary."""
    op.execute("DROP TABLE public.staged_import_state_events")
    op.execute("DROP TABLE public.staged_import_validation_results")
    op.execute("DROP TABLE public.staged_import_rows")
    op.execute("DROP TABLE public.staged_import_files")
    op.execute("DROP TABLE public.staged_imports")
    op.execute("DROP FUNCTION public.enforce_staged_import_state_transition()")
