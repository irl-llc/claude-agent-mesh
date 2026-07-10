# Head-only formula; the repo doubles as a Homebrew tap:
#   brew tap irl-llc/agent-mesh https://github.com/irl-llc/claude-agent-mesh
#   brew install --HEAD irl-llc/agent-mesh/claude-agent-mesh
class ClaudeAgentMesh < Formula
  desc "Flat, human-orchestrated mesh for independent Claude Code sessions"
  homepage "https://github.com/irl-llc/claude-agent-mesh"
  license "GPL-3.0-only"
  head "https://github.com/irl-llc/claude-agent-mesh.git", branch: "main"

  def install
    libexec.install "claude_agent_mesh.py", "mesh_wrapper.py", "mesh_runtime.py", "wire.py"
    chmod 0755, libexec/"claude_agent_mesh.py"
    chmod 0755, libexec/"mesh_wrapper.py"
    bin.install_symlink libexec/"claude_agent_mesh.py" => "claude-agent-mesh"
    bin.install_symlink libexec/"mesh_wrapper.py" => "claude-agent-mesh-wrapper"
  end

  test do
    assert_match "peer messaging", shell_output("#{bin}/claude-agent-mesh --help")
  end
end
