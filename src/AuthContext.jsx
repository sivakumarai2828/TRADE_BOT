import { createContext, useContext } from "react";

const OWNER_EMAIL = "sivakumarai2828@gmail.com";

export const AuthContext = createContext(null);

export function useAuth() {
  return useContext(AuthContext);
}

export function isOwnerEmail(email) {
  return email === OWNER_EMAIL;
}
