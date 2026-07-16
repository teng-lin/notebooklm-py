import request from "./request"

export interface LoginRequest {
  username: string
  password: string
}

export interface RegisterRequest {
  username: string
  password: string
  display_name?: string
}

export interface UserInfo {
  id: number
  username: string
  display_name: string
  avatar_url: string | null
  google_bound: boolean
  created_at: string
}

export interface AuthResponse {
  token: string
  user: UserInfo
}

export function loginApi(data: LoginRequest): Promise<AuthResponse> {
  return request.post("/api/auth/login", data).then((r) => r.data)
}

export function registerApi(data: RegisterRequest): Promise<AuthResponse> {
  return request.post("/api/auth/register", data).then((r) => r.data)
}

export function logoutApi(): Promise<void> {
  return request.post("/api/auth/logout").then((r) => r.data)
}

export function fetchMeApi(): Promise<UserInfo> {
  return request.get("/api/auth/me").then((r) => r.data)
}

export function bindGoogleApi(code: string): Promise<UserInfo> {
  return request.post("/api/auth/google/bind", { code }).then((r) => r.data)
}
