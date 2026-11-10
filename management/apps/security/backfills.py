"""
Data migrations, kept importable and out of the migrations package.

Two reasons this is not next to the migration that runs it. Django loads every
module under migrations/ as a migration and rejects one without a Migration
class, so a helper cannot live there at all. And a function defined inside a
numbered migration can only be imported by that number, which ties every
caller to a filename whose whole purpose is to be renumbered.

The migration imports from here, and so do the tests -- which is the point:
the test exercises the code that will run, not a restatement of it.
"""


def give_every_existing_account_the_role_it_already_had(apps, schema_editor):
    """
    Nobody's access changes on upgrade.

    Before this migration the panel had two levels: a superuser could do
    everything, and any other signed-in account could do everything except
    change the security policy. That second level is exactly what `operator`
    now means, so every existing non-superuser becomes one.

    Doing nothing instead would have been the safer-looking choice and the
    wrong one. An account with no profile holds no role and is refused, so
    skipping the backfill would lock out every operator the moment this
    deployed -- including, on an installation where the only accounts are
    non-superusers, everyone.

    Superusers are left without a profile on purpose. role_of() answers ADMIN
    from the Django flag before it looks for one, so a row would be a second
    source of truth for something already decided.
    """
    User = apps.get_model("auth", "User")
    OperatorProfile = apps.get_model("security", "OperatorProfile")

    OperatorProfile.objects.bulk_create(
        [
            OperatorProfile(user_id=pk, role="operator")
            for pk in User.objects.filter(is_superuser=False).values_list(
                "pk", flat=True
            )
        ],
        ignore_conflicts=True,
    )


def drop_the_profiles(apps, schema_editor):
    """Reversing drops the table anyway; this keeps the migration reversible."""
    apps.get_model("security", "OperatorProfile").objects.all().delete()
