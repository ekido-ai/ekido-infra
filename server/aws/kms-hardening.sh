#!/usr/bin/env bash
# Our own KMS key for Ekido's data at rest, and HTTPS-only buckets. Run from an operator machine with AWS access:
#   bash kms-hardening.sh            # make the changes (safe to run again: it skips what is already done)
#   DRY_RUN=1 bash kms-hardening.sh  # only print what it would do
#
# 1. A symmetric KMS key, alias/ekido-data, with yearly automatic rotation.
# 2. The live server's role (ekido-server) may use that key, and only that key: its presigned checkpoint links are
#    signed by the role, so without kms:Decrypt / GenerateDataKey every provider's download and upload would fail.
# 3. Both buckets default to SSE-KMS with that key and S3 Bucket Keys (KMS calls stay in cents).
#    Not done on purpose: refusing PUTs without an SSE header. Presigned uploads don't send one; the default
#    encryption already gives every new object our key.
# 4. Bucket policies refuse any request that isn't HTTPS.
# 5. Existing objects are re-encrypted in place (a server-side copy onto themselves). Nothing is deleted.
set -euo pipefail
REGION=ap-south-1
ALIAS=alias/ekido-data
ROLE=ekido-server
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKETS=("ekido-checkpoints-$ACCOUNT" "ekido-backups-$ACCOUNT")
run() { if [ "${DRY_RUN:-0}" = 1 ]; then echo "would run: $*"; else "$@"; fi; }

echo "== 1. KMS key"
KEY=$(aws kms describe-key --region $REGION --key-id $ALIAS --query KeyMetadata.Arn --output text 2>/dev/null || true)
if [ -z "$KEY" ] || [ "$KEY" = None ]; then
  if [ "${DRY_RUN:-0}" = 1 ]; then echo "would create the key and $ALIAS"; KEY="arn:aws:kms:$REGION:$ACCOUNT:key/DRY-RUN"
  else
    KEY=$(aws kms create-key --region $REGION --description "Ekido data at rest (checkpoints, backups)" \
      --tags TagKey=project,TagValue=ekido --query KeyMetadata.Arn --output text)
    aws kms create-alias --region $REGION --alias-name $ALIAS --target-key-id "$KEY"
  fi
fi
run aws kms enable-key-rotation --region $REGION --key-id "$KEY"
echo "key: ${KEY##*/} (rotation on)"

echo "== 2. the live server's role may use this key only"
POLICY=$(printf '{"Version":"2012-10-17","Statement":[{"Sid":"EkidoDataKey","Effect":"Allow","Action":["kms:Encrypt","kms:Decrypt","kms:GenerateDataKey","kms:DescribeKey"],"Resource":"%s"}]}' "$KEY")
run aws iam put-role-policy --role-name $ROLE --policy-name ekido-data-key --policy-document "$POLICY"
[ "${DRY_RUN:-0}" = 1 ] || sleep 15   # IAM changes take a few seconds to reach S3 and KMS

for B in "${BUCKETS[@]}"; do
  echo "== 3-4. $B: default SSE-KMS with a bucket key, HTTPS only"
  ENC=$(printf '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms","KMSMasterKeyID":"%s"},"BucketKeyEnabled":true}]}' "$KEY")
  run aws s3api put-bucket-encryption --bucket "$B" --server-side-encryption-configuration "$ENC"
  TLS=$(printf '{"Version":"2012-10-17","Statement":[{"Sid":"HttpsOnly","Effect":"Deny","Principal":"*","Action":"s3:*","Resource":["arn:aws:s3:::%s","arn:aws:s3:::%s/*"],"Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}' "$B" "$B")
  run aws s3api put-bucket-policy --bucket "$B" --policy "$TLS"

  echo "== 5. $B: re-encrypt existing objects in place"
  n=0
  while IFS=$'\t' read -r key sse; do
    [ -n "$key" ] || continue
    [ "$sse" = "aws:kms" ] && continue
    run aws s3api copy-object --bucket "$B" --key "$key" --copy-source "$B/$key" \
      --server-side-encryption aws:kms --ssekms-key-id "$KEY" --bucket-key-enabled --metadata-directive COPY --checksum-algorithm SHA256 >/dev/null
    n=$((n + 1))
  done < <(aws s3api list-objects-v2 --bucket "$B" --query 'Contents[].[Key]' --output text 2>/dev/null | grep -v '^None$' | while read -r k; do
             printf '%s\t%s\n' "$k" "$(aws s3api head-object --bucket "$B" --key "$k" --query ServerSideEncryption --output text 2>/dev/null)"; done)
  echo "$B: $n object(s) re-encrypted"
done
echo "done"
