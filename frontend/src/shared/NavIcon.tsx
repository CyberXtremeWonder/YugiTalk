const paths: Record<string, string> = {
  chat: "M4 4h16v12H9l-5 4V4Z",
  agents:
    "M8 7h8a4 4 0 0 1 4 4v7H4v-7a4 4 0 0 1 4-4ZM12 3v4M8 12h.01M16 12h.01M9 16h6",
  skills: "m12 3 9 5-9 5-9-5 9-5Zm-9 5v9l9 5 9-5V8M12 13v9",
  tools:
    "M14 4a6 6 0 0 0-7 8L3 17a3 3 0 0 0 4 4l5-5a6 6 0 0 0 8-7l-4 4-5-5 3-4Z",
  mcp: "M5 5h5v5H5V5Zm9 0h5v5h-5V5ZM5 14h5v5H5v-5Zm9 0h5v5h-5v-5ZM10 7h4M7 10v4M17 10v4M10 17h4",
  knowledge:
    "M12 5C8 2 4 3 2 4v15c3-1 6-1 10 1m0-15c4-3 8-2 10-1v15c-3-1-6-1-10 1V5Z",
  tasks: "M8 4H5v17h14V4h-3M8 2h8v5H8V2Zm0 10h8m-8 4h5",
};

/** 统一侧栏线性图标，避免不同平台的字符图标改变外观。 */
export function NavIcon({ name }: { name: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="19"
      height="19"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
}
