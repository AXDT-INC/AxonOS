// This plugin is installed root-owned in the assistant backend's isolated
// OpenCode config. OpenCode itself keeps the locked-down HOME/XDG paths, while
// approved shell tools behave like commands launched by the desktop user.
export default async () => ({
  "shell.env": async (_input, output) => {
    Object.assign(output.env, {
      HOME: "/home/aXonian",
      USER: "aXonian",
      LOGNAME: "aXonian",
      SHELL: "/bin/bash",
      XDG_CONFIG_HOME: "/home/aXonian/.config",
      XDG_DATA_HOME: "/home/aXonian/.local/share",
      XDG_STATE_HOME: "/home/aXonian/.local/state",
      XDG_CACHE_HOME: "/home/aXonian/.cache",
      DISPLAY: ":0",
      XAUTHORITY: "/home/aXonian/.Xauthority",
      VGL_DISPLAY: ":0",
    })

    // Do not leak the service's managed OpenCode loader settings into a nested
    // CLI that the user explicitly asks the agent to run.
    for (const key of [
      "OPENCODE_CONFIG",
      "OPENCODE_CONFIG_CONTENT",
      "OPENCODE_DISABLE_PROJECT_CONFIG",
      "OPENCODE_PURE",
      "OPENCODE_DISABLE_EXTERNAL_SKILLS",
      "OPENCODE_DISABLE_CLAUDE_CODE",
      "OPENCODE_DISABLE_AUTOUPDATE",
    ]) {
      // OpenCode merges this overlay over process.env after the hook returns,
      // so an empty value is required to mask rather than merely delete it.
      output.env[key] = ""
    }
  },
})
