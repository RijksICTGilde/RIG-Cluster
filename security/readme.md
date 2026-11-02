Also when testing, we can not save password, but setting up full management
is tricky as well.

Since we already use sops for infra encoding files, we will also use this for encoding passwords.

The idea is as follows:
- we create an age-key, as we do for all namespaces, but this one is created in the argo-cd namespace, manually on creation
- the operation manager can read this secret and uses it to decode password strings
-

# Generate new key pair. Use -o, not a redirect: age-keygen -o writes the file with mode 600,
# a redirect leaves it readable for group and others.
age-keygen -o key.txt

# Create secret from key.txt file
kubectl create secret generic sops-age-key --from-file=key=key.txt

# Extract private key
grep "AGE-SECRET-KEY" key.txt > private.key

# Extract public key
grep "Public key:" key.txt | cut -d' ' -f3 > public.key
# Or just copy the age1... string after "Public key:"

# Encrypt with public key
echo "secret" | age -r -a $(cat public.key)

# Decrypt with private key
age -d -i private.key encrypted.age

# Only the owner may read a key or token file
#
# The scripts and the Taskfile refuse a key or token file that group or others can read or
# write, the same rule ssh applies to a private key. The refusal names the path, the mode and
# the command to fix it:
chmod 600 key.txt
