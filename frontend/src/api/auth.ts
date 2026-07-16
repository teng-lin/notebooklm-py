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
  return request.post("/api/auth/login", data).then(async (r) => {
    const { access_token } = r.data
    localStorage.setItem("token", access_token)
    const user = await fetchMeApi()
    return { token: access_token, user }
  })
}

export function registerApi(data: RegisterRequest): Promise<AuthResponse> {
  return request.post("/api/auth/register", data).then(async (r) => {
    const { access_token } = r.data
    localStorage.setItem("token", access_token)
    const user = await fetchMeApi()
    return { token: access_token, user }
  })
}

export function logoutApi(): Promise<void> {
  return request.post("/api/auth/logout").then((r) => r.data)
}

export function fetchMeApi(): Promise<UserInfo> {
  return request.get("/api/auth/me").then((r) => r.data)
}

export function bindGoogleApi(credential: string): Promise<UserInfo> {
  return request.post("/api/auth/google/bind", { credential }).then((r) => r.data)
}
