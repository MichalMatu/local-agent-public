/* GitHub-first Local Agent Chat Bridge.
 * Local Agent/Conversation Fabric owns GitHub admission and results;
 * this extension only creates children, injects prompts and posts transport ACKs.
 * No legacy DOM campaign manager or reply collector.
 * Optional Auto Wake is an isolated Chrome timer, never a task scheduler.
 */
importScripts(
  "github_fabric_dispatch_model.js",
  "worker_github_first_urls.js",
  "worker_spawn.js",
  "worker_github_first_local_bridge.js",
  "auto_wake.js",
  "worker_github_first_events.js"
);
