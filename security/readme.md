Also when testing, we can not save password, but setting up full management
is tricky as well.

Since we already use sops for infra encoding files, we will also use this for encoding passwords.

The idea is as follows:
- we create an age-key, as we do for all namespaces, but this one is created in the argo-cd namespace, manually on creation
- the operation manager can read this secret and uses it to decode password strings
- 

# Generate new key pair
age-keygen > key.txt

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