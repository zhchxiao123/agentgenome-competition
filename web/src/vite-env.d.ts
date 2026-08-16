/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端地址。同源部署时留空——多一层配置就多一处会配错的地方。 */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
