import axios from 'axios';

const auth = axios.create({
  baseURL: '/api/auth',
});

class Auth {
  static async login(creds) {
    return auth
      .post('/', creds)
      .then((resp) => resp.data)
      .catch((err) => {
        throw err;
      });
  }

  static async refresh(rfshToken) {
    return auth
      .post('/refresh', { data: { refresh_token: rfshToken } })
      .then((resp) => resp.data);
  }
}

export default Auth;
