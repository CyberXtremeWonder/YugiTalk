import { useState } from "react";
import { createRoot } from "react-dom/client";
import { ChatView } from "../../src/chat/ChatView";
import type { Account } from "../../src/accounts/api";
import "../../src/shared/styles.css";
import "../../src/shared/hermes.css";

const noop = () => {};
function Harness() {
  const [value, setValue] = useState("");
  return (
    <ChatView
      value={value}
      onChange={setValue}
      agentVersion="version"
      identity={{
        user: {
          id: "user",
          email: "user@test",
          name: "测试",
          platform_admin: false,
        },
        csrf_token: "csrf",
      }}
      account={{ id: "user" } as Account}
      conversationId="conversation"
      onConversation={noop}
      onRun={noop}
    />
  );
}
createRoot(document.getElementById("root")!).render(<Harness />);
