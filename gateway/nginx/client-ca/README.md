# Client certificates for the control panel

Put the CA bundle that signs your operator certificates here, and point the
panel's access policy at `/etc/nginx/client-ca/<filename>`.

This is the strongest control available for a publicly reachable panel: a
caller without a certificate signed by this CA is refused during the TLS
handshake. They never reach the sign-in form, so a stolen password — or a
password and a stolen phone — is worth nothing on its own.

The cost is real: every operator needs a certificate installed in the browser
they use, and losing a device means revoking and reissuing. That is a fair
trade for a surface that controls every domain the gateway serves, but it is a
trade.

## Issuing certificates

`make panel-ca` creates the CA, and `make panel-cert NAME=alice` issues one
operator certificate as a `.p12` bundle to import into a browser.

Nothing in here is generated automatically, and the CA's private key is never
written into a container image. Keep `panel-ca.key` somewhere you would keep a
root password; anyone holding it can mint their own access.
