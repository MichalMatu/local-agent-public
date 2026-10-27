# Frequently asked questions

## Does Codex have to stay open?

No. Codex installs and verifies the native service. Local Agent then runs
independently. ChatGPT/GitHub access is used to publish and read tasks.

## Is the public repository my task inbox?

No. Keep commands and results in your own private Git repository. Public
source, private transport and private local state are separate.

## Does my computer have to be online?

Yes, to fetch new tasks and publish results. An unavailable selected machine
is not silently replaced by another host.

## Can one installation work without a project?

Yes. Host command tasks do not require a project repository. Projects are
optional, explicitly configured execution targets.

## What happens after a crash or Git outage?

Saved results can be republished without running commands again. Interrupted
or uncertain effects remain UNKNOWN and require inspection. Narrow, verified
Git metadata recovery handles specific known cases; arbitrary files are not
removed automatically.

## Is Local Agent a sandbox?

No. Authorized private Git writers can publish commands with the service
OS user's permissions. See [security](SECURITY_MODEL.md).

## Can I use Windows or expect Termux to run forever?

Windows is not implemented. Android background execution depends on device
policy and requires separate endurance testing. See [support](SUPPORT.md).

## How do I pause or remove it?

Use the installed platform's STOP/disable action, inspect active work and use
the native service manager. Preserve configuration, keys and task history.
Follow [operations](OPERATIONS.md).
