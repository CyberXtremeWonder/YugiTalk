export interface Account {
  id: string;
  name: string;
  role: "owner" | "admin" | "member";
}
