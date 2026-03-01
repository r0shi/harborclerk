import 'react'

declare module 'react' {
  interface InputHTMLAttributes<T> {
    // Apple password rules — tells Safari/Passwords.app the password policy
    // https://developer.apple.com/documentation/security/customizing-password-autofill-rules
    passwordrules?: string
  }
}
