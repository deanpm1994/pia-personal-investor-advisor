"""Add profile ownership and deny-by-default row-level security.

Revision ID: 20260714_02
Revises: 20260713_01
Create Date: 2026-07-14 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260714_02"
down_revision: str | Sequence[str] | None = "20260713_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the application-owned profile boundary without altering auth."""
    op.execute(
        """
        CREATE TABLE public.profiles (
            id uuid PRIMARY KEY REFERENCES auth.users (id) ON DELETE CASCADE,
            email text,
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            updated_at timestamptz NOT NULL DEFAULT timezone('utc', now())
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.sync_profile_from_auth_user()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public
        AS $$
        BEGIN
            INSERT INTO public.profiles (id, email)
            VALUES (NEW.id, NEW.email)
            ON CONFLICT (id) DO UPDATE
            SET email = EXCLUDED.email,
                updated_at = timezone('utc', now());
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER on_auth_user_created_or_updated
        AFTER INSERT OR UPDATE OF email ON auth.users
        FOR EACH ROW EXECUTE FUNCTION public.sync_profile_from_auth_user()
        """
    )
    op.execute(
        """
        INSERT INTO public.profiles (id, email)
        SELECT id, email FROM auth.users
        ON CONFLICT (id) DO UPDATE
        SET email = EXCLUDED.email,
            updated_at = timezone('utc', now())
        """
    )
    op.execute("ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON public.profiles FROM PUBLIC, anon, authenticated")
    op.execute("GRANT SELECT ON public.profiles TO authenticated")
    op.execute(
        """
        CREATE POLICY profiles_select_own ON public.profiles
        FOR SELECT TO authenticated
        USING ((SELECT auth.uid()) = id)
        """
    )


def downgrade() -> None:
    """Remove the P2.3 profile boundary and its Auth synchronization hook."""
    op.execute("DROP TRIGGER on_auth_user_created_or_updated ON auth.users")
    op.execute("DROP FUNCTION public.sync_profile_from_auth_user()")
    op.execute("DROP TABLE public.profiles")
