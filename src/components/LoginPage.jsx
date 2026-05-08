import { useGoogleLogin } from "@react-oauth/google";
import { useState } from "react";
import { isOwnerEmail } from "../AuthContext.jsx";

export default function LoginPage({ onLogin }) {
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const login = useGoogleLogin({
    onSuccess: async (tokenResponse) => {
      setLoading(true);
      setError(null);
      try {
        const res = await fetch("https://www.googleapis.com/oauth2/v3/userinfo", {
          headers: { Authorization: `Bearer ${tokenResponse.access_token}` },
        });
        const user = await res.json();
        const authUser = {
          email: user.email,
          name: user.name,
          picture: user.picture,
          isOwner: isOwnerEmail(user.email),
        };
        localStorage.setItem("auth_user", JSON.stringify(authUser));
        onLogin(authUser);
      } catch {
        setError("Login failed. Try again.");
        setLoading(false);
      }
    },
    onError: () => {
      setError("Google login failed. Try again.");
      setLoading(false);
    },
  });

  return (
    <div className="flex min-h-screen items-center justify-center bg-neutral-950">
      <div className="w-full max-w-sm rounded-2xl border border-neutral-800 bg-neutral-900 p-8 shadow-2xl">
        {/* Logo / title */}
        <div className="mb-8 text-center">
          <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-xl bg-emerald-500/10 border border-emerald-500/20">
            <svg className="h-7 w-7 text-emerald-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
                d="M3 13.5l4-4 4 4 4-6 4 3" />
            </svg>
          </div>
          <h1 className="text-xl font-semibold text-white">AI Trading Bot</h1>
          <p className="mt-1 text-sm text-neutral-400">Sign in to access dashboard</p>
        </div>

        {/* Google button */}
        <button
          onClick={() => { setError(null); login(); }}
          disabled={loading}
          className="flex w-full items-center justify-center gap-3 rounded-xl border border-neutral-700 bg-white px-4 py-3 text-sm font-medium text-neutral-900 transition hover:bg-neutral-100 disabled:opacity-50"
        >
          <svg className="h-5 w-5" viewBox="0 0 24 24">
            <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
            <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
            <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l3.66-2.84z"/>
            <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
          </svg>
          {loading ? "Signing in…" : "Continue with Google"}
        </button>

        {error && (
          <p className="mt-4 rounded-lg bg-red-500/10 border border-red-500/20 px-3 py-2 text-center text-sm text-red-400">
            {error}
          </p>
        )}

        <p className="mt-6 text-center text-xs text-neutral-600">
          Authorized users only
        </p>
      </div>
    </div>
  );
}
