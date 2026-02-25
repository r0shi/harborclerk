import SwiftUI

struct LoginView: View {
    @EnvironmentObject private var authManager: AuthManager

    let errorMessage: String?

    @State private var email = ""
    @State private var password = ""
    @State private var rememberMe = true
    @FocusState private var focusedField: Field?

    private enum Field: Hashable {
        case email, password
    }

    var body: some View {
        VStack(spacing: 0) {
            Spacer()

            VStack(spacing: 24) {
                // App icon + title
                VStack(spacing: 12) {
                    Image(nsImage: NSApp.applicationIconImage)
                        .resizable()
                        .frame(width: 80, height: 80)

                    Text("Harbor Clerk")
                        .font(.title)
                        .fontWeight(.semibold)
                }

                // Error message
                if let msg = errorMessage {
                    Text(msg)
                        .foregroundColor(.red)
                        .font(.callout)
                        .multilineTextAlignment(.center)
                }

                // Form fields
                VStack(spacing: 12) {
                    TextField("Email", text: $email)
                        .textFieldStyle(.roundedBorder)
                        .textContentType(.username)
                        .focused($focusedField, equals: .email)
                        .onSubmit { focusedField = .password }

                    SecureField("Password", text: $password)
                        .textFieldStyle(.roundedBorder)
                        .textContentType(.password)
                        .focused($focusedField, equals: .password)
                        .onSubmit { submit() }

                    Toggle("Remember me", isOn: $rememberMe)
                        .toggleStyle(.checkbox)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }

                // Sign in button
                Button(action: submit) {
                    Text("Sign In")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .disabled(email.isEmpty || password.isEmpty)
            }
            .frame(width: 300)

            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear {
            // Pre-fill email from Keychain if available
            if let creds = KeychainManager.load() {
                email = creds.email
            }
            focusedField = email.isEmpty ? .email : .password
        }
    }

    private func submit() {
        guard !email.isEmpty, !password.isEmpty else { return }
        Task {
            await authManager.login(email: email, password: password, rememberMe: rememberMe)
        }
    }
}
