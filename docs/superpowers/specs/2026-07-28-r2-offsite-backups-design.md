# Cloudflare R2 Offsite Backup Design

## Goal

Protect the production SQLite database from loss of the Zeabur persistent
volume by copying each verified daily backup to a private Cloudflare R2 bucket.
Operators must also be able to download and verify an offsite backup without
overwriting the live database.

## Scope

This change extends the existing `maintenance backup` workflow. It does not
change the database schema, the scheduler timing, or the live restore procedure.
Cloudflare account creation, bucket creation, credentials, and the bucket
lifecycle rule are one-time deployment configuration.

## Configuration

Remote backups are enabled only when all of these environment variables exist:

- `FUNDING_ARB_R2_ACCOUNT_ID`
- `FUNDING_ARB_R2_ACCESS_KEY_ID`
- `FUNDING_ARB_R2_SECRET_ACCESS_KEY`
- `FUNDING_ARB_R2_BUCKET`

The R2 endpoint is derived from the account ID. Credentials remain in protected
Zeabur variables and are never logged or committed. The bucket is private and
has a 30-day object-expiration lifecycle rule.

## Backup Flow

`maintenance backup` continues to create an online SQLite backup locally and
run `PRAGMA integrity_check` on it. If R2 configuration is present, the command
then uploads the verified file under:

`funding-arb-monitor/YYYY/MM/DD/<filename>`

The upload includes a SHA-256 checksum as object metadata. The command prints
the local backup path and the remote object key, but no credentials.

If R2 configuration is absent, manual local backups keep their current
behavior. In production, all four R2 variables will be configured, so the
existing scheduled backup uploads offsite automatically. Partial configuration
is an error.

An upload failure makes the command exit nonzero. The existing scheduler records
that failure, exposes it through readiness and status, and sends its configured
failure alert. The verified local backup remains available for diagnosis.

## Restore Flow

A new `maintenance download --key KEY --destination PATH` command downloads one
R2 object to a new local path. It verifies the stored SHA-256 metadata and runs
SQLite integrity checking before reporting success. It refuses to overwrite an
existing file.

Downloading never replaces the configured live database. An operator must stop
the service, preserve the current database, and explicitly copy the verified
download into place using the documented restore procedure.

## Dependencies

Add `boto3` as the sole runtime dependency for the S3-compatible R2 client and
AWS Signature Version 4 authentication. This avoids maintaining custom
security-sensitive request-signing code.

## Tests

Unit tests use a fake in-memory S3 client; they do not contact Cloudflare.
Coverage includes:

- no remote upload when no R2 variables are configured;
- rejection of partial R2 configuration;
- upload key, checksum metadata, and private upload behavior;
- upload failure propagation;
- successful download with checksum and SQLite integrity verification;
- checksum mismatch, invalid SQLite, and existing-destination rejection;
- CLI output and exit behavior.

The existing full test suite and container smoke test must continue to pass.

## Deployment and Verification

1. Create a private R2 bucket and a token restricted to object read/write for
   that bucket.
2. Configure a 30-day object-expiration lifecycle rule.
3. Add the four protected R2 variables to the Zeabur service.
4. Deploy the tested commit.
5. Trigger one manual backup inside the service.
6. Confirm the object exists in R2 and the application remains ready.
7. Download that object to a temporary path with the new command and confirm
   both checksum and SQLite integrity verification succeed.

Success means a verified database copy exists outside Zeabur and can be
downloaded and validated without touching the live database.
