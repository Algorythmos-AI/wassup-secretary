import { formatPhone, humanizeIntent, maskPhone } from "./format";

it("humanises intents and phone numbers", () => {
  expect(humanizeIntent("callback_request")).toBe("Callback request");
  expect(humanizeIntent(null)).toBe("Call");
  expect(formatPhone("+61412345678")).toBe("0412 345 678");
  expect(formatPhone("+61238211140")).toBe("(02) 3821 1140");
  expect(formatPhone("+61755501234")).toBe("(07) 5550 1234");
  expect(formatPhone("+6412345678")).toBe("+6412345678"); // not Australian: left as given
  expect(formatPhone("+612382111")).toBe("+612382111"); // wrong length: left as given
  expect(formatPhone(null)).toBe("Number withheld");
  expect(maskPhone("+61412345678")).toBe("ends 678");
});
